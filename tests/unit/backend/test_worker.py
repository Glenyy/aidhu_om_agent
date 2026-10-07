"""S04-04 唯一 worker 与队列认领的单测。

覆盖 [plan/10 §4—§8](../../../plan/10-任务状态与恢复设计.md) 里可自动化的部分：

- 独占锁同一时刻只允许一个持有者；**锁失败不改队列状态**（CLI 退出码 3）；
- 队列按 `created_at, job_id` 串行认领，认领即占执行槽并把批次改为 running；
- 判别闸门打开时任务**保留在队列里**，不被消费也不被标失败；
- 启动恢复把 `running` 调用改为 `unknown_after_interrupt`（占用旧预算）、
  `running` 任务改为 `interrupted`、释放执行槽、批次改为 `interrupted`；
- 批次终态按 §8 汇总：全 completed → completed；有失败行 → partial_failed；
  认证/配置类错误 → failed 且暂停派发；
- 输入失败行不调用模型，且照样让批次落到 partial_failed；
- 已 completed 的记录在再次消费时**不重复调用**；
- 记录数不守恒时拒绝写终态（不猜标签）。

全部使用合成工作簿、本地 SQLite 与模拟客户端，**零真实模型调用**。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aidhu_om_agent.agent.mock_samples import MockClient
from aidhu_om_agent.llm.errors import ModelConfigError
from aidhu_om_agent.repositories import jobs as jobs_repo
from aidhu_om_agent.repositories import records as records_repo
from aidhu_om_agent.repositories import runs as runs_repo
from aidhu_om_agent.schemas.qa import QARecord
from aidhu_om_agent.services.batches import create_run
from aidhu_om_agent.storage import Database, utc_now
from aidhu_om_agent.worker import (
    SYSTEMIC_FAILURE_CODES,
    Worker,
    WorkerError,
    WorkerLock,
    WorkerLockError,
    WorkerStateError,
)
from fixtures.excel_samples import partial_failure_workbook, qa_row, write_workbook
from test_batches import make_config, prepare_validation

STAMP = "2026-10-06T00:00:00+00:00"


# ------------------------------------------------------------------ 测试夹具


def three_valid_workbook(path: Path) -> Path:
    """3 条**全部有效且都有资料**的记录：模拟「回答正确」时摘录一定有出处。"""
    return write_workbook(
        path,
        [
            qa_row("1", "问题一", "回答一", ["资料一"]),
            qa_row("2", "问题二", "回答二", ["资料二"]),
            qa_row("3", "问题三", "回答三", ["资料三"]),
        ],
    )


class ConfigInvalidClient:
    """实现 `llm.client.ModelClient`：一调用就报配置无效（认证类故障）。"""

    def __init__(self, record: QARecord) -> None:
        self.record = record
        self.calls: list[str] = []

    def call(self, messages, stage):  # noqa: ANN001, ANN201 - 只用于抛错
        self.calls.append(stage)
        raise ModelConfigError("缺少服务地址或凭据", stage=stage)


def correct_factory(calls: list[str] | None = None):
    """固定「回答正确」情境的客户端工厂；顺带记录被叫醒的记录顺序。"""

    def factory(record: records_repo.RecordRow) -> MockClient:
        if calls is not None:
            calls.append(record.record_key)
        return MockClient(record.to_qa_record(), scenario="correct")

    return factory


def make_worker(config, **kwargs) -> Worker:
    return Worker(config, poll_seconds=0.01, sleep=lambda _: None, **kwargs)


def prepare_run(config, workbook: Path, tmp_path: Path):
    """走「上传 → 预检 → 建批次」，返回 (database, created)。"""
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    return database, create_run(database, config, validation_id=validation_id)


class Probe:
    """短命连接读写助手；**每次都新开连接**，避免测试自己制造长事务。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def one(self, sql: str, parameters: tuple[object, ...] = ()):
        connection = self._database.connect()
        try:
            return connection.execute(sql, parameters).fetchone()
        finally:
            connection.close()

    def all(self, sql: str, parameters: tuple[object, ...] = ()) -> list:
        connection = self._database.connect()
        try:
            return list(connection.execute(sql, parameters))
        finally:
            connection.close()

    def do(self, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for sql, parameters in statements:
                connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def scalar(self, sql: str, parameters: tuple[object, ...] = ()):
        return self.one(sql, parameters)[0]


# ------------------------------------------------------------------ 独占锁


def test_lock_admits_one_holder_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    first, second = WorkerLock(path), WorkerLock(path)

    assert first.acquire(owner="w-1") is True
    assert first.held is True
    assert second.acquire(owner="w-2") is False
    assert second.held is False

    first.release()
    assert first.held is False
    # 锁文件保留：删掉它会让后来的进程锁到不同的 inode。
    assert path.exists()
    assert second.acquire(owner="w-2") is True
    second.release()


def test_lock_file_holds_owner_metadata(tmp_path: Path) -> None:
    """归属信息只是给人看的；**判断独占靠的是操作系统锁**。"""
    path = tmp_path / "worker.lock"
    holder = WorkerLock(path)
    assert holder.acquire(owner="w-holder") is True
    try:
        # 持锁期间归属信息仍然**可读**：锁在文件尾部，不在开头。
        text = path.read_text(encoding="utf-8")
        assert "worker_id=w-holder" in text and f"pid={os.getpid()}" in text
        assert WorkerLock(path).acquire(owner="w-other") is False
    finally:
        holder.release()


def test_second_worker_start_leaves_the_queue_untouched(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    first = make_worker(config)
    first.start()
    try:
        started_at = probe.scalar("SELECT worker_started_at FROM runtime_state")

        second = make_worker(config)
        with pytest.raises(WorkerLockError):
            second.start()

        # 队列与运行时状态都没有被第二个 worker 碰过。
        assert probe.scalar("SELECT status FROM jobs") == "queued"
        assert probe.scalar("SELECT status FROM runs") == "queued"
        assert probe.scalar("SELECT worker_id FROM runtime_state") == first.worker_id
        assert probe.scalar("SELECT worker_started_at FROM runtime_state") == started_at
        assert second.recovery is None
        assert second.worker_id != first.worker_id
    finally:
        first.stop()

    assert created.job_id


def test_cli_worker_reports_lock_conflict_with_exit_code(tmp_path, monkeypatch) -> None:
    """CLI 层：锁被占用时以退出码 3 结束，**不启动消费循环**。"""
    from aidhu_om_agent import cli

    config = make_config(tmp_path)
    holder = WorkerLock(config.paths.runtime / "worker.lock")
    assert holder.acquire(owner="w-holder") is True
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    try:
        assert cli.main(["worker", "--once"]) == cli.EXIT_LOCK_HELD
    finally:
        holder.release()


def test_worker_rejects_unknown_mode(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(WorkerError) as error:
        make_worker(config, mode="pretend")
    assert "pretend" in str(error.value)


# ------------------------------------------------------------------ 队列认领


def test_claim_takes_the_oldest_job_and_sets_the_slot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, older = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "a.xlsx"), tmp_path
    )
    _, newer = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "b.xlsx"), tmp_path
    )
    probe = Probe(database)

    # 两个批次在同一秒内建成，`created_at` 相同；把后一个改成明显更晚的时间，
    # 让「按 created_at, job_id 排序」有唯一确定的答案。
    probe.do(
        [
            (
                "UPDATE jobs SET created_at = '9999-01-01T00:00:00+00:00' WHERE job_id = ?",
                (newer.job_id,),
            )
        ]
    )

    worker = make_worker(config)
    worker.start()
    try:
        job = worker._claim()
        assert job is not None and job.job_id == older.job_id
        assert job.status == "running" and job.worker_slot == jobs_repo.WORKER_SLOT
        assert job.worker_id == worker.worker_id
        # 认领同时把**被认领那个批次**改为 running 并落 started_at。
        assert probe.scalar(
            "SELECT status FROM runs WHERE run_id = ?", (older.run_id,)
        ) == "running"
        assert probe.scalar(
            "SELECT started_at FROM runs WHERE run_id = ?", (older.run_id,)
        ) is not None
    finally:
        worker.stop()

    # 更晚的任务原样留在队列里，没被动过。
    assert probe.scalar(
        "SELECT status FROM jobs WHERE job_id = ?", (newer.job_id,)
    ) == "queued"
    assert probe.scalar(
        "SELECT status FROM runs WHERE run_id = ?", (newer.run_id,)
    ) == "queued"


def test_pause_keeps_queued_jobs_out_of_consumption(tmp_path: Path) -> None:
    """闸门打开时**任务仍在队列里**，没有被消费，也没有被标成失败。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    # 直接构造 worker 但不调 start()：start() 会解除上一轮暂停（配置检查通过），
    # 那属于另一条已由 test_startup_resumes_paused_dispatch 覆盖的路径。
    worker = make_worker(config)
    assert worker.worker_id

    connection = database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        jobs_repo.set_dispatch_paused(
            connection,
            reason={"code": "CONFIG_INVALID", "message": "凭据缺失"},
            now=utc_now(),
        )
        connection.commit()
    finally:
        connection.close()

    assert worker._claim() is None
    assert probe.scalar("SELECT status FROM jobs") == "queued"
    assert probe.scalar("SELECT status FROM runs") == "queued"
    assert probe.scalar("SELECT worker_slot FROM jobs") is None
    assert created.job_id


def test_startup_resumes_paused_dispatch(tmp_path: Path) -> None:
    """「在配置检查后解除上轮暂停」：重启 worker 可以让排队任务重新流动。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, _ = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    connection = database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        jobs_repo.set_dispatch_paused(
            connection, reason={"code": "CONFIG_INVALID", "message": "旧凭据"}, now=utc_now()
        )
        connection.commit()
    finally:
        connection.close()
    assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 1

    worker = make_worker(config)
    worker.start()
    try:
        assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 0
        assert probe.scalar("SELECT pause_reason_json FROM runtime_state") is None
        assert worker._claim() is not None
    finally:
        worker.stop()


# ------------------------------------------------------------------ 启动恢复


def test_startup_recovery_marks_interrupted_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    connection = database.connect()
    try:
        key = records_repo.list_records(connection, created.run_id)[0].record_key
        connection.execute("BEGIN IMMEDIATE")
        # 伪造「上一轮 worker 崩溃」：任务 running 占着执行槽、批次 running，
        # 且有一次已占位但结果未提交的调用。
        connection.execute(
            "UPDATE jobs SET status = 'running', worker_id = 'w-crashed', worker_slot = 1,"
            " started_at = ? WHERE job_id = ?",
            (STAMP, created.job_id),
        )
        connection.execute(
            "UPDATE runs SET status = 'running', started_at = ? WHERE run_id = ?",
            (STAMP, created.run_id),
        )
        campaign = records_repo.open_campaign(
            connection,
            record_key=key,
            stage=1,
            max_attempts=3,
            reason=records_repo.CAMPAIGN_INITIAL,
            created_at=STAMP,
        )
        records_repo.reserve_attempt(
            connection,
            campaign_id=campaign.campaign_id,
            attempt_no=1,
            started_at=STAMP,
            job_id=created.job_id,
        )
        connection.commit()
    finally:
        connection.close()

    worker = make_worker(config)
    report = worker.start()
    try:
        assert report.attempts_marked_unknown == 1
        assert report.jobs_marked_interrupted == 1
        assert report.runs_marked_interrupted == 1
        assert report.changed is True

        assert probe.scalar("SELECT status FROM call_attempts") == "unknown_after_interrupt"
        # 中断的尝试**继续占用旧预算**：不能被静默退还。
        assert probe.scalar(
            "SELECT COUNT(*) FROM call_attempts WHERE campaign_id = ?",
            (campaign.campaign_id,),
        ) == 1

        assert probe.scalar("SELECT status FROM jobs") == "interrupted"
        assert probe.scalar("SELECT worker_slot FROM jobs") is None
        assert probe.scalar("SELECT status FROM runs") == "interrupted"
        # 本 worker 已登记。
        assert probe.scalar("SELECT worker_id FROM runtime_state") == worker.worker_id
        assert probe.scalar("SELECT worker_mode FROM runtime_state") == "mock"
    finally:
        worker.stop()


def test_recovery_without_incident_reports_no_change(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database = Database(config.paths.database)
    database.initialize()

    worker = make_worker(config)
    report = worker.start()
    try:
        assert report.changed is False
        assert (report.attempts_marked_unknown, report.jobs_marked_interrupted) == (0, 0)
    finally:
        worker.stop()


# ------------------------------------------------------------------ 批次执行


def test_worker_completes_a_batch_in_mock_mode(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        # 判别任务 + 它终态时排上的自动导出（S05-03）；导出不调用模型。
        assert worker.run_forever(max_idle_rounds=1) == 2
    finally:
        worker.stop()

    assert len(calls) == 3
    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT finished_at FROM runs") is not None
    assert probe.scalar("SELECT status FROM jobs") == "completed"
    assert probe.scalar("SELECT worker_slot FROM jobs") is None
    assert "counts" in probe.scalar("SELECT result_json FROM jobs")

    connection = database.connect()
    try:
        assert records_repo.count_by_status(connection, created.run_id) == {
            "pending": 0,
            "input_invalid": 0,
            "stage1_done": 0,
            "completed": 3,
            "failed": 0,
        }
    finally:
        connection.close()

    # 每条记录留下阶段一与阶段二结果，且带最终标签。
    assert probe.scalar("SELECT COUNT(*) FROM stage_results") == 6
    assert probe.scalar(
        "SELECT COUNT(*) FROM records WHERE final_label IS NOT NULL"
    ) == 3
    # 模拟调用的尝试在数据层可分辨（界面才能显著标识）。
    assert probe.scalar("SELECT simulated FROM call_attempts LIMIT 1") == 1


def test_two_batches_are_consumed_serially(tmp_path: Path) -> None:
    """执行槽被释放后下一个任务才能被认领；结束时没有槽被占着。"""
    config = make_config(tmp_path)
    database, first = prepare_run(config, three_valid_workbook(tmp_path / "src" / "a.xlsx"), tmp_path)
    _, second = prepare_run(config, three_valid_workbook(tmp_path / "src" / "b.xlsx"), tmp_path)
    probe = Probe(database)

    worker = make_worker(config, client_factory=correct_factory())
    worker.start()
    try:
        # 两个判别任务 + 两条自动导出（每个批次终态各排一条）。
        assert worker.run_forever(max_idle_rounds=1) == 4
    finally:
        worker.stop()

    assert [row["status"] for row in probe.all("SELECT status FROM jobs ORDER BY job_id")] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert probe.scalar("SELECT COUNT(*) FROM jobs WHERE worker_slot IS NOT NULL") == 0
    assert len(
        probe.all(
            "SELECT worker_slot FROM jobs WHERE run_id IN (?, ?)", (first.run_id, second.run_id)
        )
    ) == 4  # 每个批次两条：判别 + 自动导出


def test_input_invalid_rows_are_never_sent_to_the_model(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = partial_failure_workbook(tmp_path / "src" / "partial.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    # 4 条记录里只有 1 条有效；模型只被那条叫醒一次。
    assert len(calls) == 1
    # 有输入失败行 → partial_failed（§8「存在 input_invalid 或 failed」）。
    assert probe.scalar("SELECT status FROM runs") == "partial_failed"
    connection = database.connect()
    try:
        counts = records_repo.count_by_status(connection, created.run_id)
        assert (counts["completed"], counts["input_invalid"]) == (1, 3)
    finally:
        connection.close()
    # 尝试行挂在 campaign 上，所以要经 stage_campaigns 才能问「输入失败行有没有调用」。
    assert probe.scalar(
        "SELECT COUNT(*) FROM call_attempts a"
        " JOIN stage_campaigns c ON c.campaign_id = a.campaign_id"
        " JOIN records r ON r.record_key = c.record_key"
        " WHERE r.status = 'input_invalid'"
    ) == 0


def test_completed_records_are_not_called_again(tmp_path: Path) -> None:
    """普通恢复不重复调用已完成记录（[plan/10 §3]「保持 completed，不重复调用」）。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()
    assert len(calls) == 3

    # 人为把批次与任务退回 queued（模拟「记录都已终态、批次终态没提交」的崩溃），
    # 再次消费：三条记录都是 completed，不应该再多出任何一次模型调用。
    probe.do(
        [
            (
                "UPDATE runs SET status = 'queued', finished_at = NULL WHERE run_id = ?",
                (created.run_id,),
            ),
            (
                "UPDATE jobs SET status = 'queued', finished_at = NULL, worker_slot = NULL,"
                " error_json = NULL WHERE job_id = ?",
                (created.job_id,),
            ),
        ]
    )

    replay = make_worker(config, client_factory=correct_factory(calls))
    replay.start()
    try:
        replay.run_forever(max_idle_rounds=1)
    finally:
        replay.stop()

    assert len(calls) == 3  # 没有新增调用
    assert probe.scalar("SELECT status FROM runs") == "completed"


# ------------------------------------------------------------------ 系统性故障


def test_systemic_failure_stops_the_batch_and_pauses_dispatch(tmp_path: Path) -> None:
    """认证/配置类错误 → run.failed + 暂停派发，且**不在其余记录上重复失败**。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    made: list[ConfigInvalidClient] = []

    def factory(record: records_repo.RecordRow) -> ConfigInvalidClient:
        client = ConfigInvalidClient(record.to_qa_record())
        made.append(client)
        return client

    worker = make_worker(config, client_factory=factory)
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    # 只处理了一条就停下，不把同一次故障重复记 3 遍。
    assert len(made) == 1
    assert probe.scalar("SELECT status FROM runs") == "failed"
    assert "CONFIG_INVALID" in probe.scalar("SELECT last_error_json FROM runs")

    assert probe.scalar("SELECT status FROM jobs") == "failed"
    assert probe.scalar("SELECT worker_slot FROM jobs") is None
    assert "CONFIG_INVALID" in probe.scalar("SELECT error_json FROM jobs")

    # 闸门与终态在同一事务里落库。
    assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 1
    assert "CONFIG_INVALID" in probe.scalar("SELECT pause_reason_json FROM runtime_state")

    # 其余两条没有被调用，也没有被贴上假的失败原因。
    connection = database.connect()
    try:
        counts = records_repo.count_by_status(connection, created.run_id)
    finally:
        connection.close()
    assert counts == {
        "pending": 2,
        "input_invalid": 0,
        "stage1_done": 0,
        "completed": 0,
        "failed": 1,
    }

    # 暂停期间队列不再被消费。
    assert worker._claim() is None


def test_output_invalid_is_a_single_record_failure_not_a_pause(tmp_path: Path) -> None:
    """输出不合规是**单条**失败：其余有效行继续处理，不暂停派发。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    calls: list[str] = []

    def factory(record: records_repo.RecordRow) -> MockClient:
        calls.append(record.record_key)
        scenario = "forced_failure" if record.record_id == "2" else "correct"
        return MockClient(record.to_qa_record(), scenario=scenario)

    worker = make_worker(config, client_factory=factory)
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert len(calls) == 3  # 三条都试过，没有提前停
    assert probe.scalar("SELECT status FROM runs") == "partial_failed"
    assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 0
    connection = database.connect()
    try:
        counts = records_repo.count_by_status(connection, created.run_id)
        failed = connection.execute(
            "SELECT final_label, failure_json FROM records WHERE status = 'failed'"
        ).fetchone()
    finally:
        connection.close()
    assert (counts["completed"], counts["failed"]) == (2, 1)
    # 失败不留标签，且失败原因里能读到受控错误码。
    assert failed["final_label"] is None
    assert "OUTPUT_INVALID" in failed["failure_json"]


def test_unusable_prompt_snapshot_pauses_instead_of_crashing(tmp_path: Path) -> None:
    """批次快照不可用时**暂停并报告**，而不是让异常穿出主循环留下悬挂的 running。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    # 抹掉阶段一提示词正文：`execute_record` 会以 VERSION_INCOMPATIBLE 拒绝。
    probe.do(
        [
            (
                "UPDATE runs SET prompt_snapshot_json = '{\"stage1\": {}, \"stage2\": {}}'"
                " WHERE run_id = ?",
                (created.run_id,),
            )
        ]
    )

    worker = make_worker(config, client_factory=correct_factory())
    worker.start()
    try:
        assert worker.run_forever(max_idle_rounds=1) == 0  # 没有任何任务被算作成功
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "failed"
    assert "VERSION_INCOMPATIBLE" in probe.scalar("SELECT last_error_json FROM runs")
    assert probe.scalar("SELECT status FROM jobs") == "failed"
    assert probe.scalar("SELECT worker_slot FROM jobs") is None  # 槽已释放
    assert probe.scalar("SELECT model_dispatch_paused FROM runtime_state") == 1
    # 没有调用模型，也没有写出半个标签。
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 0
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE final_label IS NOT NULL") == 0


def test_run_status_follows_plan_section_8() -> None:
    """§8 的口径逐条核对；不靠「看起来差不多」。"""
    from aidhu_om_agent.worker import _run_status

    assert _run_status(completed=3, failed=0, valid=3, input_invalid=0) == "completed"
    assert _run_status(completed=1, failed=0, valid=1, input_invalid=3) == "partial_failed"
    assert _run_status(completed=2, failed=1, valid=3, input_invalid=0) == "partial_failed"
    assert _run_status(completed=0, failed=3, valid=3, input_invalid=0) == "partial_failed"


def test_systemic_codes_match_the_plan_taxonomy() -> None:
    """暂停集合只含认证/配置类；输出类错误**必须**留在外面。"""
    assert SYSTEMIC_FAILURE_CODES == {"CONFIG_INVALID", "MODEL_PERMANENT"}
    for code in ("OUTPUT_INVALID", "CONTEXT_LIMIT", "OUTPUT_TRUNCATED", "MODEL_RETRYABLE"):
        assert code not in SYSTEMIC_FAILURE_CODES


# ------------------------------------------------------------------ 状态不守恒


def test_worker_refuses_to_finalize_with_unprocessed_records(tmp_path: Path) -> None:
    """记录数不守恒时**不猜终态**：报错而不是写一个好看的 completed。"""
    config = make_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)

    connection = database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE jobs SET status = 'running', worker_id = 'w-x', worker_slot = 1"
            " WHERE job_id = ?",
            (created.job_id,),
        )
        connection.commit()
        job = jobs_repo.get_job(connection, created.job_id)
    finally:
        connection.close()
    assert job is not None

    worker = make_worker(config, client_factory=correct_factory())
    with pytest.raises(WorkerStateError):
        worker._finalize(job, attempted=1)

    # 没有写终态：批次还是 queued、任务还是 running（留给恢复处理）。
    assert probe.scalar("SELECT status FROM runs") == "queued"
    assert probe.scalar("SELECT status FROM jobs") == "running"
    assert probe.scalar("SELECT finished_at FROM runs") is None


def test_run_forever_idles_when_the_queue_is_empty(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database = Database(config.paths.database)
    database.initialize()

    worker = make_worker(config)
    worker.start()
    try:
        assert worker.run_forever(max_idle_rounds=1) == 0
    finally:
        worker.stop()
    assert Probe(database).scalar("SELECT COUNT(*) FROM jobs") == 0


# ------------------------------------------------------------------ 真实子进程


def test_worker_cli_drains_an_empty_queue_in_a_subprocess(tmp_path: Path) -> None:
    """真跑 `python -m aidhu_om_agent worker --once`：**跨进程**锁与退出码成立。

    子进程用显式环境变量指向临时目录，不读仓库里的真实配置与数据库。
    """
    database = Database(tmp_path / "runtime" / "state.sqlite3")
    database.initialize()

    completed = subprocess.run(
        [sys.executable, "-m", "aidhu_om_agent", "worker", "--once", "--poll-seconds", "0.05"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(tmp_path),
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "本次消费任务数：0" in completed.stdout

    # 退出后锁被释放：同一进程里可以立刻再拿到。
    probe = WorkerLock(tmp_path / "runtime" / "worker.lock")
    assert probe.acquire(owner="w-probe") is True
    probe.release()
    # 子进程确实登记过运行时状态。
    assert Probe(database).scalar("SELECT COUNT(*) FROM runtime_state") == 1


def _child_env(tmp_path: Path) -> dict[str, str]:
    """指向临时目录的最小环境；**不写入任何凭据**。"""
    runtime = tmp_path / "runtime"
    env = dict(os.environ)
    env.update(
        {
            "AIDHU_DATABASE_PATH": str(runtime / "state.sqlite3"),
            "AIDHU_RUNTIME_PATH": str(runtime),
            "AIDHU_UPLOADS_PATH": str(runtime / "uploads"),
            "AIDHU_OUTPUTS_PATH": str(tmp_path / "outputs"),
            "AIDHU_LOGS_PATH": str(tmp_path / "logs"),
            "AIDHU_FRONTEND_DIST_PATH": str(tmp_path / "dist"),
            # 配置里必须能构造出模型配置；模拟模式不会真的调用它。
            "AIDHU_STAGE1_BASE_URL": "https://example.invalid/v1",
            "AIDHU_STAGE1_MODEL": "unused-in-mock",
            "AIDHU_STAGE2_BASE_URL": "https://example.invalid/v1",
            "AIDHU_STAGE2_MODEL": "unused-in-mock",
        }
    )
    return env
