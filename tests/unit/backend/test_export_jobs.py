"""S05-03 worker 侧导出任务的单测。

覆盖：

- 判别终态（`completed`/`partial_failed`）**在同一事务里**排一条自动导出；
- `failed` 终态**不排**导出（没有可交付的分类结论）；
- 自动导出被 worker 消费、两份文件成对登记、路径与摘要可回库核对；
- 导出任务失败**只标任务自己**：批次不改、分类结果不改、不打开判别闸门；
- 判别闸门打开时**导出照常消费**，判别任务仍留在队列里。

全程模拟模式、本地 SQLite，**零真实调用**。
"""

from __future__ import annotations

from pathlib import Path

from aidhu_om_agent.schemas.export import ARTIFACT_EXCEL, ARTIFACT_JSONL
from aidhu_om_agent.services.exports import (
    create_manual_export,
    schedule_automatic_export,
)
from aidhu_om_agent.storage import write_transaction
from aidhu_om_agent.worker import EXPORT_ERROR_LIMIT, Worker, export_error_message
from test_batches import make_config
from test_worker import (
    ConfigInvalidClient,
    Probe,
    correct_factory,
    make_worker,
    prepare_run,
    three_valid_workbook,
)

STAMP = "2026-10-07T00:00:00+00:00"


def run_forever(worker: Worker, *, idle_rounds: int = 1) -> int:
    worker.start()
    try:
        return worker.run_forever(max_idle_rounds=idle_rounds)
    finally:
        worker.stop()


def insert_manual_export(database, run_id: str) -> tuple[str, str]:
    """排一条**手动**导出（走 `create_manual_export`，即界面上点导出那条路径）。

    自动导出按 `revision` 去重，同一 revision 排不了第二条，所以「队列里既有导出
    又有判别」这种组合用手动导出来搭。
    """
    created = create_manual_export(database, run_id)
    return created.job_id, created.export_id


# ------------------------------------------------------- 终态排自动导出（S05-03）


def test_terminal_state_schedules_exactly_one_automatic_export(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    assert run_forever(make_worker(config, client_factory=correct_factory())) == 2

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'export' AND mode = 'automatic'"
    ) == 1
    row = probe.one("SELECT * FROM exports WHERE run_id = ?", (created.run_id,))
    assert row["source"] == "automatic"
    # 快照在**认领时**捕获：捕获信息已回填，且与终态一致。
    assert row["captured_at"] and row["captured_run_status"] == "completed"
    assert row["captured_revision"] is not None
    assert probe.scalar(
        "SELECT COUNT(*) FROM artifacts WHERE export_id = ?", (row["export_id"],)
    ) == 2


def test_automatic_export_files_land_under_the_run_directory(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    run_forever(make_worker(config, client_factory=correct_factory()))

    export = probe.one("SELECT * FROM exports WHERE run_id = ?", (created.run_id,))
    rows = probe.all(
        "SELECT * FROM artifacts WHERE export_id = ? ORDER BY kind", (export["export_id"],)
    )
    assert [row["kind"] for row in rows] == [ARTIFACT_EXCEL, ARTIFACT_JSONL]
    for row in rows:
        path = Path(row["relative_path"])
        assert not path.is_absolute()  # 存相对路径，换机器也能读
        absolute = config.paths.outputs / path
        assert absolute.is_file()
        assert absolute.stat().st_size == row["size_bytes"]
        # 下载名只由程序生成的 id 组成：来源文件名不进路径、不进下载名。
        assert row["download_name"] == absolute.name
        assert created.run_id[:8] in row["download_name"]
        assert row["media_type"].startswith("application/")


def test_a_failed_run_does_not_schedule_an_export(tmp_path: Path) -> None:
    """系统性故障的批次没有可交付结论，不该排一份空导出误导用户。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do(
        [
            (
                "UPDATE runs SET prompt_snapshot_json = '{\"stage1\": {}, \"stage2\": {}}'"
                " WHERE run_id = ?",
                (created.run_id,),
            )
        ]
    )

    assert run_forever(make_worker(config, client_factory=correct_factory())) == 0

    assert probe.scalar("SELECT status FROM runs") == "failed"
    assert probe.scalar("SELECT COUNT(*) FROM exports") == 0
    assert probe.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'export'") == 0
    assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 1


def test_each_terminal_revision_is_scheduled_at_most_once(tmp_path: Path) -> None:
    """同一 revision 上重复安排只留一条：靠 `uq_automatic_export_revision`。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    run_forever(make_worker(config, client_factory=correct_factory()))
    probe = Probe(database)
    revision = int(probe.scalar("SELECT revision FROM runs WHERE run_id = ?", (created.run_id,)))

    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            assert (
                schedule_automatic_export(
                    tx, run_id=created.run_id, revision=revision, created_at=STAMP
                )
                is None
            )
    finally:
        connection.close()

    assert probe.scalar("SELECT COUNT(*) FROM exports") == 1
    assert probe.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'export'") == 1


# ----------------------------------------------------- 导出失败只标任务（S05-03）


def test_export_failure_marks_only_the_job(tmp_path: Path) -> None:
    """导出失败不改批次、不动分类结果、不打开判别闸门。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    # 先只跑判别：导出任务排上了但还留在队列里。
    worker = make_worker(config, client_factory=correct_factory())
    worker.start()
    try:
        assert worker.run_once() is not None  # 判别
    finally:
        worker.stop()

    # 抽掉 exports 行：导出任务找不到自己的登记，`execute_export` 会拒绝执行。
    probe.do([("DELETE FROM exports WHERE run_id = ?", (created.run_id,))])
    export_job_id = str(
        probe.scalar("SELECT job_id FROM jobs WHERE kind = 'export'")
    )

    assert run_forever(make_worker(config, client_factory=correct_factory())) == 0

    job = probe.one("SELECT * FROM jobs WHERE job_id = ?", (export_job_id,))
    assert job["status"] == "failed"
    assert job["worker_slot"] is None
    assert "EXPORT_FAILED" in job["error_json"]
    # 批次与分类结果原封不动。
    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE final_label IS NOT NULL") == 3
    assert probe.scalar("SELECT COUNT(*) FROM artifacts") == 0
    # 判别闸门**不因导出失败而关**：它管的是模型派发。
    assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 0


# --------------------------------------------- 暂停判别时仍消费导出（S05-03）


def test_paused_dispatch_still_consumes_export_jobs(tmp_path: Path) -> None:
    """闸门只挡判别：已排上的导出照常跑完，判别任务留在队列里。

    闸门在 **worker 运行期间** 才设：`Worker.start()` 会按设计解除上一轮的暂停
    （那是「配置检查通过后重新允许派发」），所以启动后再设才有意义。
    """
    config = make_config(tmp_path)
    database, first = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "a.xlsx"), tmp_path
    )
    probe = Probe(database)
    run_forever(make_worker(config, client_factory=correct_factory()))

    # 第二个批次只入队、不消费，用来证明暂停时判别任务确实被留着。
    _, second = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "b.xlsx"), tmp_path
    )
    export_job_id, export_id = insert_manual_export(database, first.run_id)

    worker = make_worker(config, client_factory=correct_factory())
    worker.start()
    try:
        probe.do(
            [
                (
                    "INSERT INTO runtime_state (singleton_id, model_dispatch_paused,"
                    " updated_at) VALUES (1, 1, ?)"
                    " ON CONFLICT (singleton_id) DO UPDATE SET model_dispatch_paused = 1",
                    (STAMP,),
                )
            ]
        )
        assert worker.run_forever(max_idle_rounds=1) == 1  # 只消费了那一条导出
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM jobs WHERE job_id = ?", (export_job_id,)) == (
        "completed"
    )
    assert probe.scalar(
        "SELECT COUNT(*) FROM artifacts WHERE export_id = ?", (export_id,)
    ) == 2
    # 判别任务仍在队列里，批次也还没被推成 running。
    assert probe.scalar("SELECT status FROM jobs WHERE run_id = ?", (second.run_id,)) == (
        "queued"
    )
    assert probe.scalar(
        "SELECT status FROM runs WHERE run_id = ?", (second.run_id,)
    ) == "queued"


def test_claiming_an_export_job_leaves_the_run_untouched(tmp_path: Path) -> None:
    """导出任务认领时**不**动批次：状态与 revision 都不变。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    run_forever(make_worker(config, client_factory=correct_factory()))

    before = probe.one("SELECT status, revision FROM runs WHERE run_id = ?", (created.run_id,))
    assert before["status"] == "completed"
    job_id, export_id = insert_manual_export(database, created.run_id)

    worker = make_worker(config, client_factory=correct_factory())
    worker.start()
    try:
        assert worker.run_once() is not None
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM jobs WHERE job_id = ?", (job_id,)) == "completed"
    after = probe.one("SELECT status, revision FROM runs WHERE run_id = ?", (created.run_id,))
    assert (after["status"], after["revision"]) == (before["status"], before["revision"])
    # 第二份导出是**独立**的一份：不与自动导出共用 artifact 行。
    assert probe.scalar(
        "SELECT COUNT(*) FROM artifacts WHERE export_id = ?", (export_id,)
    ) == 2
    assert probe.scalar("SELECT COUNT(*) FROM artifacts") == 4


# ----------------------------------------------------- 客户端不被导出叫醒（S05-03）


def test_export_job_never_calls_the_model(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        worker.run_once()  # 判别
        assert len(calls) == 3
        worker.run_once()  # 导出
    finally:
        worker.stop()

    assert len(calls) == 3  # 导出没有新增任何调用
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 6
    assert probe.scalar("SELECT COUNT(*) FROM artifacts") == 2


def test_config_invalid_client_never_reaches_the_export_path(tmp_path: Path) -> None:
    """认证类故障在判别阶段就停住：不会走到导出，也不会有半份产物。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    assert run_forever(make_worker(config, client_factory=ConfigInvalidClient)) == 0

    assert probe.scalar("SELECT status FROM runs") == "failed"
    assert probe.scalar("SELECT COUNT(*) FROM exports") == 0
    assert probe.scalar("SELECT COUNT(*) FROM artifacts") == 0


# ------------------------------------------- 导出错误信息不带本机路径（S05-03）


def test_export_error_message_strips_the_outputs_root_in_every_shape(
    tmp_path: Path,
) -> None:
    """导出失败的原因会被 `GET /api/runs/{id}/exports` 原样发给浏览器。

    **这里必须用真实的 OS 异常造消息**：`OSError.__str__` 用 `repr()` 拼
    `filename`，Windows 路径的反斜杠在字符串里是两个字符，只按 `str(path)`
    替换会永远不命中——正是这个形态曾经漏过本机路径。
    """
    root = tmp_path / "outputs"
    for blocked, label in ((root, "outputs 根"), (root / "run1" / "exp1", "子目录")):
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("占位文件", encoding="utf-8")
        try:
            (blocked / "sub").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message = export_error_message(exc, root)
        else:  # pragma: no cover - 平台差异导致没抛异常时不该静默通过
            raise AssertionError(f"{label}：mkdir 没有按预期失败")
        assert str(root) not in message, message
        assert str(root).replace("\\", "\\\\") not in message, message
        assert str(root).replace("\\", "/") not in message, message
        assert str(tmp_path) not in message, message
        assert "run1" not in message and "exp1" not in message, message
        blocked.unlink()


def test_export_error_message_keeps_readable_text_and_shortens_it() -> None:
    """换成占位符之后仍要说人话；普通文本里的斜杠不能被当路径吃掉。"""
    root = Path("D:/somewhere/outputs")

    assert export_error_message(RuntimeError("快照不自洽，缺少 stage1 结果"), root) == (
        "快照不自洽，缺少 stage1 结果"
    )
    # `1/2`、`/runs` 这类正常文本原样保留，只换真正的绝对路径。
    text = export_error_message(RuntimeError("5/10 条缺证据，见 /runs 页面"), root)
    assert text == "5/10 条缺证据，见 /runs 页面"

    # 只留第一行，并截断到上限：整段堆栈不进任务行。
    long_exc = RuntimeError("首行说明\n第二行堆栈")
    assert export_error_message(long_exc, root) == "首行说明"
    assert len(export_error_message(RuntimeError("啊" * 500), root)) == EXPORT_ERROR_LIMIT


def test_export_error_message_replaces_a_stray_local_path() -> None:
    """导出根之外的绝对路径（如另一处磁盘故障）同样不发给浏览器。"""
    root = Path("D:/somewhere/outputs")

    message = export_error_message(OSError("无法写入 D:/data/uploads/abc.xlsx"), root)

    assert "uploads" not in message and "D:" not in message, message
