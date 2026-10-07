"""S04-05 普通恢复与显式重试的单测。

覆盖 [plan/10 §6](../../../plan/10-任务状态与恢复设计.md) 与
[plan/08 §7](../../../plan/08-API接口与数据合同.md)：

- 可选记录的选择规则：`pending`／`stage1_done` 总可选；`failed` 只在**失败阶段
  还有剩余预算**时可选；`completed`／`input_invalid` 不纳入；`CONTEXT_LIMIT`、
  `OUTPUT_TRUNCATED` 与 `retry_failed` 无关，只能新建批次；
- **普通恢复不重置次数**：继续用旧 campaign 的剩余次数，不新开轮次；
- **显式重试才开新一轮**：`campaign_no` 加一、原因 `explicit_retry`、上限不变；
- 重开计划**入队时只记录、认领时才落实**；同一任务重复落实不叠加轮次；
- `NOTHING_TO_RESUME`／`STATE_CONFLICT`／`VERSION_INCOMPATIBLE` 三条拒绝路径；
- `finalize_only` 零模型调用；
- 恢复沿用**批次快照**的提示词（不读磁盘当前内容）。

全部使用合成工作簿、本地 SQLite 与模拟客户端，**零真实模型调用**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aidhu_om_agent.agent.mock_samples import SCENARIO_FORCED_FAILURE, MockClient
from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.repositories import jobs as jobs_repo
from aidhu_om_agent.repositories import records as records_repo
from aidhu_om_agent.repositories import runs as runs_repo
from aidhu_om_agent.schemas.qa import QARecord
from aidhu_om_agent.services.batches import (
    NEW_BATCH_REQUIRED_CODES,
    BatchError,
    allowed_actions,
    plan_resume,
    resume_run,
)
from aidhu_om_agent.storage import Database, utc_now, write_transaction
from test_batches import make_config
from test_worker import Probe, correct_factory, make_worker, prepare_run, three_valid_workbook

STAGE2 = "stage2"


# ------------------------------------------------------------------ 测试夹具


class FlakyStage2:
    """阶段二前 ``fail_times`` 次返回**被拒的输出**，之后返回合规输出。

    用一次「先失败、后成功」的客户端在零真实调用下造出「预算用尽的技术失败」，
    再验证 `retry_failed` 重开新轮次后确实能救回来。计数用工厂外的共享字典，
    这样两次 worker 运行看到的是同一份调用历史。
    """

    def __init__(self, record: QARecord, *, fail_times: int, seen: dict[str, int]) -> None:
        self._qa = record
        self._key = record.record_id or f"row:{record.source_row}"
        self._fail_times = fail_times
        self._seen = seen
        self._good = MockClient(record, scenario="correct")
        self._bad = MockClient(record, scenario=SCENARIO_FORCED_FAILURE)

    def call(self, messages, stage):  # noqa: ANN001, ANN201 - 测试替身
        if stage != STAGE2:
            return self._good.call(messages, stage)
        used = self._seen.get(self._key, 0)
        self._seen[self._key] = used + 1
        if used < self._fail_times:
            return self._bad.call(messages, stage)
        return self._good.call(messages, stage)


def flaky_factory(*, fail_times: int, seen: dict[str, int]):
    def factory(record: records_repo.RecordRow) -> FlakyStage2:
        return FlakyStage2(record.to_qa_record(), fail_times=fail_times, seen=seen)

    return factory


def exhaust_stage2_budget(config, tmp_path: Path) -> tuple[object, object, Probe, dict[str, int]]:
    """让 3 条记录的阶段二各失败 3 次：批次 partial_failed、预算全部用尽。"""
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    probe = Probe(database)
    seen: dict[str, int] = {}

    worker = make_worker(config, client_factory=flaky_factory(fail_times=3, seen=seen))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "partial_failed"
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'failed'") == 3
    assert probe.scalar(
        "SELECT COUNT(*) FROM call_attempts a JOIN stage_campaigns c"
        " ON c.campaign_id = a.campaign_id WHERE c.stage = 2 AND a.status = 'failed'"
    ) == 9
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE final_label IS NOT NULL") == 0
    return database, created, probe, seen


def plan(config, database, run_id: str, *, retry_failed: bool):  # noqa: ANN001
    connection = database.connect()
    try:
        return plan_resume(connection, run_id, retry_failed=retry_failed)
    finally:
        connection.close()


# ------------------------------------------------------------------ 选择规则


def test_pending_records_are_always_selectable(tmp_path: Path) -> None:
    """全新批次（记录全是 pending）普通恢复即可选中，且不重开任何轮次。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do([("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,))])

    planned = plan(config, database, created.run_id, retry_failed=False)

    assert len(planned.selected_records) == 3
    assert planned.renewed_campaigns == 0
    assert planned.skipped_budget_exhausted == 0
    assert planned.finalize_only is False and planned.has_work is True


def test_completed_and_input_invalid_rows_are_never_selected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do(
        [
            ("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,)),
            (
                "UPDATE records SET status = 'completed', final_label = '回答正确',"
                " review_required = 0 WHERE order_index = 0",
                (),
            ),
            (
                "UPDATE records SET status = 'input_invalid', input_error_json = '{}'"
                " WHERE order_index = 1",
                (),
            ),
        ]
    )

    planned = plan(config, database, created.run_id, retry_failed=True)

    assert len(planned.selected_records) == 1
    rows = probe.all("SELECT order_index FROM records WHERE status IN ('completed','input_invalid')")
    assert sorted(row["order_index"] for row in rows) == [0, 1]


def test_exhausted_budget_is_reported_and_not_selectable_without_retry(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created, probe, _ = exhaust_stage2_budget(config, tmp_path)

    planned = plan(config, database, created.run_id, retry_failed=False)

    assert planned.selected_records == ()
    assert planned.renewed_campaigns == 0
    assert planned.skipped_budget_exhausted == 3
    assert planned.finalize_only is False
    assert planned.has_work is False
    assert "预算已用尽" in planned.reason_when_empty

    with pytest.raises(BatchError) as error:
        resume_run(database, run_id=created.run_id)
    assert error.value.code == "NOTHING_TO_RESUME"
    assert error.value.http_status == 409
    assert error.value.details["skipped_budget_exhausted"] == 3
    # 被拒绝时不留任何任务与幂等记录。
    assert probe.scalar("SELECT COUNT(*) FROM jobs") == 1
    assert probe.scalar("SELECT COUNT(*) FROM idempotency_keys") == 0


def test_new_batch_codes_are_not_selectable_even_with_retry(tmp_path: Path) -> None:
    """`CONTEXT_LIMIT`／`OUTPUT_TRUNCATED` 只能新建批次，不算「重开就能救」。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do(
        [
            ("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,)),
            (
                "UPDATE records SET status = 'failed', failure_stage = 'stage1',"
                " failure_json = '{\"code\": \"CONTEXT_LIMIT\", \"message\": \"超限\"}'",
                (),
            ),
            # 真实流程里这些失败行会被 worker 收尾成 partial_failed；这里补上同一个
            # 终态，避免测出「queued + 全部失败行」这种现实中不存在的组合。
            (
                "UPDATE runs SET status = 'partial_failed', finished_at = ? WHERE run_id = ?",
                (utc_now(), created.run_id),
            ),
        ]
    )

    planned = plan(config, database, created.run_id, retry_failed=True)

    assert planned.selected_records == ()
    assert planned.skipped_needs_new_batch == 3
    assert planned.skipped_budget_exhausted == 0
    assert "新建批次" in planned.reason_when_empty
    with pytest.raises(BatchError) as error:
        resume_run(database, run_id=created.run_id, retry_failed=True)
    assert error.value.code == "NOTHING_TO_RESUME"

    assert NEW_BATCH_REQUIRED_CODES == {"CONTEXT_LIMIT", "OUTPUT_TRUNCATED"}


# ------------------------------------------------------ 普通恢复不重置次数


def _simulate_crash_during_first_stage1_attempt(database, created, probe: Probe) -> str:
    """造出「阶段一第 1 次调用占位后被杀死」的真实形状。

    用**真实的** `jobs_repo.recover_interrupted_state` 做转换，而不是手写一个
    乐观的未知状态；返回那条记录的主键。
    """
    record_key = probe.scalar(
        "SELECT record_key FROM records ORDER BY order_index LIMIT 1"
    )
    campaign_id = "c" * 32
    probe.do(
        [
            (
                "UPDATE jobs SET status = 'running', worker_id = 'w-dead', worker_slot = 1"
                " WHERE job_id = ?",
                (created.job_id,),
            ),
            ("UPDATE runs SET status = 'running' WHERE run_id = ?", (created.run_id,)),
            (
                "INSERT INTO stage_campaigns (campaign_id, record_key, stage, campaign_no,"
                " max_attempts, reason, created_by_job_id, created_at)"
                " VALUES (?, ?, 1, 1, 3, 'initial', ?, ?)",
                (campaign_id, record_key, created.job_id, utc_now()),
            ),
            (
                "INSERT INTO call_attempts (attempt_id, campaign_id, job_id, attempt_no,"
                " status, started_at) VALUES (?, ?, ?, 1, 'running', ?)",
                ("a" * 32, campaign_id, created.job_id, utc_now()),
            ),
            (
                "UPDATE records SET status = 'failed', failure_stage = 'stage1',"
                " failure_json = '{\"code\": \"MODEL_RETRYABLE\", \"message\": \"网络中断\"}'"
                " WHERE record_key = ?",
                (record_key,),
            ),
        ]
    )

    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            report = jobs_repo.recover_interrupted_state(tx, now=utc_now())
    finally:
        connection.close()

    assert report.attempts_marked_unknown == 1
    assert probe.scalar(
        "SELECT status FROM call_attempts WHERE attempt_id = ?", ("a" * 32,)
    ) == "unknown_after_interrupt"
    assert probe.scalar("SELECT status FROM runs") == "interrupted"
    return record_key


def test_ordinary_resume_reuses_the_old_campaign(tmp_path: Path) -> None:
    """中断后剩余预算仍可用 → 普通恢复选中它，且**不新开轮次**。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    record_key = _simulate_crash_during_first_stage1_attempt(database, created, probe)

    planned = plan(config, database, created.run_id, retry_failed=False)
    assert planned.selected_records == tuple(
        row["record_key"] for row in probe.all("SELECT record_key FROM records ORDER BY order_index")
    )
    assert planned.renewed_campaigns == 0 and planned.skipped_budget_exhausted == 0

    resumed = resume_run(database, run_id=created.run_id, idempotency_key="k1", request_sha256="d1")
    assert (resumed.selected_records, resumed.renewed_campaigns) == (3, 0)
    assert resumed.finalize_only is False and resumed.reused is False
    # 入队阶段**不落实**重开计划：轮次表里没有 explicit_retry。
    assert probe.scalar(
        "SELECT COUNT(*) FROM stage_campaigns WHERE reason = 'explicit_retry'"
    ) == 0

    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert len(calls) == 3
    assert probe.scalar("SELECT status FROM runs") == "completed"
    # 那条记录继续用第 1 轮 campaign（campaign_no=1），尝试序号接着往下走。
    attempts = probe.all(
        "SELECT a.attempt_no, a.status FROM call_attempts a JOIN stage_campaigns c"
        " ON c.campaign_id = a.campaign_id WHERE c.record_key = ? AND c.stage = 1"
        " ORDER BY a.attempt_no",
        (record_key,),
    )
    assert [(row["attempt_no"], row["status"]) for row in attempts] == [
        (1, "unknown_after_interrupt"),
        (2, "succeeded"),
    ]
    assert probe.scalar(
        "SELECT COUNT(*) FROM stage_campaigns WHERE record_key = ? AND stage = 1", (record_key,)
    ) == 1


# ------------------------------------------------------ 显式重试才重开预算


def test_retry_failed_reopens_one_campaign_per_row_and_rescues_them(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created, probe, seen = exhaust_stage2_budget(config, tmp_path)

    planned = plan(config, database, created.run_id, retry_failed=True)
    assert len(planned.selected_records) == 3
    assert planned.renewed_campaigns == 3
    assert all(stage == 2 for _, stage in planned.reopen)

    before = probe.one("SELECT created_at, revision FROM runs WHERE run_id = ?", (created.run_id,))
    resumed = resume_run(
        database, run_id=created.run_id, retry_failed=True, idempotency_key="k1", request_sha256="d1"
    )
    assert (resumed.selected_records, resumed.renewed_campaigns) == (3, 3)
    assert resumed.finalize_only is False
    # 计划只写在任务里；此时还没有新轮次。
    assert probe.scalar("SELECT COUNT(*) FROM stage_campaigns") == 6
    (job,) = probe.all("SELECT * FROM jobs WHERE job_id = ?", (resumed.job_id,))
    payload = json.loads(job["payload_json"])
    assert job["mode"] == "retry_failed"
    assert len(payload["record_keys"]) == 3 and payload["retry_failed"] is True
    assert sorted(len(stages) for stages in payload["reopen"].values()) == [1, 1, 1]
    assert all(stages == [2] for stages in payload["reopen"].values())
    # 批次回到 queued：终止时间清空，创建时间不变，revision 递增。
    (run,) = probe.all("SELECT * FROM runs")
    assert (run["status"], run["finished_at"]) == ("queued", None)
    assert run["created_at"] == before["created_at"]
    assert run["revision"] == before["revision"] + 1

    worker = make_worker(config, client_factory=flaky_factory(fail_times=3, seen=seen))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'completed'") == 3
    assert probe.scalar(
        "SELECT COUNT(*) FROM stage_campaigns WHERE stage = 2 AND reason = 'explicit_retry'"
        " AND created_by_job_id = ?",
        (resumed.job_id,),
    ) == 3
    # 新一轮只有 1 次尝试；旧一轮的 3 次仍然保留在账上。
    assert probe.scalar(
        "SELECT COUNT(*) FROM call_attempts a JOIN stage_campaigns c"
        " ON c.campaign_id = a.campaign_id WHERE c.stage = 2"
    ) == 12
    assert probe.scalar(
        "SELECT MAX(campaign_no) FROM stage_campaigns WHERE stage = 2"
    ) == 2
    assert probe.scalar(
        "SELECT MAX(max_attempts) FROM stage_campaigns WHERE reason = 'explicit_retry'"
    ) == 3  # 上限不变，不是「重置成更大的值」


def test_reopen_is_applied_once_per_job(tmp_path: Path) -> None:
    """同一任务的重开计划**不重复落实**：任务被再次恢复时轮次不叠加。"""
    config = make_config(tmp_path)
    database, created, probe, _ = exhaust_stage2_budget(config, tmp_path)
    resumed = resume_run(
        database, run_id=created.run_id, retry_failed=True, idempotency_key="k1", request_sha256="d1"
    )

    worker = make_worker(config)
    worker.start()
    try:
        job = worker._claim()
        assert job is not None and job.job_id == resumed.job_id
        assert worker._apply_reopen(job) == 3
        # 模拟「重开完成、任务执行中被中断、同一任务再次落实」。
        assert worker._apply_reopen(job) == 0
    finally:
        worker.stop()

    assert probe.scalar("SELECT COUNT(*) FROM stage_campaigns") == 9
    assert probe.scalar(
        "SELECT MAX(campaign_no) FROM stage_campaigns WHERE stage = 2"
    ) == 2


def test_resume_conflicts_with_an_active_job(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )

    # 刚创建的批次自带一个 queued 判别任务：恢复必须先等它结束。
    with pytest.raises(BatchError) as error:
        resume_run(database, run_id=created.run_id)
    assert error.value.code == "STATE_CONFLICT"
    assert error.value.http_status == 409

    connection = database.connect()
    try:
        with pytest.raises(BatchError) as error:
            plan_resume(connection, created.run_id, retry_failed=True)
    finally:
        connection.close()
    assert error.value.code == "STATE_CONFLICT"


def test_resume_rejects_an_unusable_prompt_snapshot(tmp_path: Path) -> None:
    """快照不可用即拒绝入队，而不是入队后让 worker 挂掉（§0.1 恢复沿用快照）。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do(
        [
            ("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,)),
            (
                "UPDATE runs SET prompt_snapshot_json = ? WHERE run_id = ?",
                (json.dumps({"stage1": {}, "stage2": {"text": "x", "sha256": "y"}}), created.run_id),
            ),
        ]
    )

    with pytest.raises(BatchError) as error:
        resume_run(database, run_id=created.run_id)
    assert error.value.code == "VERSION_INCOMPATIBLE"
    assert error.value.details["stage"] == "stage1"
    assert probe.scalar("SELECT COUNT(*) FROM jobs") == 1


def test_resume_of_unknown_run_is_not_found(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database = Database(config.paths.database)
    database.initialize()

    with pytest.raises(BatchError) as error:
        resume_run(database, run_id="0" * 32)
    assert (error.value.code, error.value.http_status) == ("NOT_FOUND", 404)


# ---------------------------------------------------------------- 仅收尾


def test_finalize_only_calls_no_model(tmp_path: Path) -> None:
    """记录都已终态、只差批次收尾：零模型调用，且不新增尝试行。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
        assert len(calls) == 3

        # 模拟「记录都提交了，批次终态没提交」：退回 queued，与中断后的形状一致。
        job_id = probe.scalar("SELECT job_id FROM jobs ORDER BY created_at LIMIT 1")
        probe.do(
            [
                (
                    "UPDATE runs SET status = 'interrupted', finished_at = NULL WHERE run_id = ?",
                    (created.run_id,),
                ),
                (
                    "UPDATE jobs SET status = 'interrupted', finished_at = NULL,"
                    " worker_slot = NULL WHERE job_id = ?",
                    (job_id,),
                ),
            ]
        )

        resumed = resume_run(database, run_id=created.run_id, idempotency_key="k", request_sha256="d")
        assert resumed.finalize_only is True
        assert resumed.selected_records == 0 and resumed.renewed_campaigns == 0

        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert len(calls) == 3  # 收尾没有新增任何模型调用
    assert probe.scalar(
        "SELECT COUNT(*) FROM call_attempts"
    ) == 6  # 3 条 × 2 阶段，只有最初那 6 次
    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT finished_at FROM runs") is not None


# ---------------------------------------------------------------- 幂等键


def test_same_resume_key_replays_the_original_result(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created, probe, _ = exhaust_stage2_budget(config, tmp_path)

    first = resume_run(
        database, run_id=created.run_id, retry_failed=True, idempotency_key="k1", request_sha256="d1"
    )
    again = resume_run(
        database, run_id=created.run_id, retry_failed=True, idempotency_key="k1", request_sha256="d1"
    )

    assert again.reused is True
    assert again.job_id == first.job_id
    assert again.response_data()["selected_records"] == first.selected_records
    assert probe.scalar("SELECT COUNT(*) FROM jobs") == 2  # 只有最初与恢复各一条


def test_same_resume_key_with_a_different_body_conflicts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do([("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,))])
    resume_run(database, run_id=created.run_id, idempotency_key="k1", request_sha256="d1")

    with pytest.raises(BatchError) as error:
        resume_run(database, run_id=created.run_id, retry_failed=True, idempotency_key="k1",
                   request_sha256="d2")

    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert probe.scalar("SELECT COUNT(*) FROM jobs") == 2


def test_resume_idempotency_scope_is_per_run(tmp_path: Path) -> None:
    """同一幂等键用在两个批次上互不影响（范围按批次区分）。"""
    config = make_config(tmp_path)
    database, first = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "a.xlsx"), tmp_path
    )
    _, second = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "b.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do([("UPDATE jobs SET status = 'completed'", ())])

    one = resume_run(database, run_id=first.run_id, idempotency_key="shared", request_sha256="d1")
    two = resume_run(database, run_id=second.run_id, idempotency_key="shared", request_sha256="d1")

    assert one.job_id != two.job_id
    scopes = {row["scope"] for row in probe.all("SELECT scope FROM idempotency_keys")}
    assert scopes == {
        f"POST /api/runs/{first.run_id}/resume",
        f"POST /api/runs/{second.run_id}/resume",
    }


def test_resume_response_shape_matches_the_contract(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do([("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,))])

    data = resume_run(
        database, run_id=created.run_id, idempotency_key="k1", request_sha256="d1"
    ).response_data()

    assert set(data) == {
        "run_id",
        "job_id",
        "selected_records",
        "renewed_campaigns",
        "skipped_budget_exhausted",
        "skipped_needs_new_batch",
        "finalize_only",
        "dispatch_paused",
        "reused",
    }
    assert isinstance(data["dispatch_paused"], bool)
    # 响应里不出现任何凭据或提示词正文。
    assert "prompt" not in json.dumps(data) and "key" not in json.dumps(data)


def test_allowed_actions_uses_the_same_planning(tmp_path: Path) -> None:
    """界面侧的 `allowed_actions` 与提交路径共用规划，不会「说能恢复却拒绝」。"""
    config = make_config(tmp_path)
    database, created, probe, _ = exhaust_stage2_budget(config, tmp_path)

    connection = database.connect()
    try:
        actions = allowed_actions(connection, created.run_id)
    finally:
        connection.close()

    assert actions["can_resume"] is False
    assert actions["can_retry_failed"] is True
    assert actions["skipped_budget_exhausted"] == 3
    assert actions["retry_failed_selected"] == 3
    assert actions["retry_failed_renewed"] == 3
    assert actions["remaining_rows"] == 0
    assert actions["disabled_reason"] is not None
    with pytest.raises(BatchError) as error:
        resume_run(database, run_id=created.run_id)
    assert error.value.code == "NOTHING_TO_RESUME"


def test_resume_end_to_end_through_http_and_worker(tmp_path: Path) -> None:
    """整条链路：预算用尽 → HTTP 显式重试入队 → worker 落实重开 → 批次完成。"""
    config = make_config(tmp_path)
    database, created, probe, seen = exhaust_stage2_budget(config, tmp_path)

    client = TestClient(create_app(config))
    response = client.post(
        f"/api/runs/{created.run_id}/resume",
        json={"retry_failed": True},
        headers={"Idempotency-Key": "e2e-key"},
    )
    assert response.status_code == 202
    data = response.json()["data"]
    assert (data["selected_records"], data["renewed_campaigns"]) == (3, 3)

    worker = make_worker(config, client_factory=flaky_factory(fail_times=3, seen=seen))
    worker.start()
    try:
        assert worker.run_forever(max_idle_rounds=1) == 1
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'completed'") == 3
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE final_label IS NOT NULL") == 3
    assert probe.scalar(
        "SELECT COUNT(*) FROM stage_campaigns WHERE reason = 'explicit_retry'"
    ) == 3
    # 全程模拟：没有任何一次真实调用混进来。
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts WHERE simulated = 0") == 0
    (job,) = probe.all("SELECT status, result_json FROM jobs WHERE job_id = ?", (data["job_id"],))
    assert job["status"] == "completed"
    assert json.loads(job["result_json"])["counts"]["failed"] == 0


def test_resume_keeps_the_batch_prompt_snapshot(tmp_path: Path) -> None:
    """恢复沿用**批次快照**的提示词摘要，而不是磁盘当前内容（§0.1）。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do([("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,))])
    connection = database.connect()
    try:
        snapshot = runs_repo.get_prompt_snapshot(connection, created.run_id)
    finally:
        connection.close()

    resume_run(database, run_id=created.run_id, idempotency_key="k1", request_sha256="d1")
    calls: list[str] = []
    worker = make_worker(config, client_factory=correct_factory(calls))
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    digests = {
        row["prompt_sha256"] for row in probe.all("SELECT prompt_sha256 FROM stage_results")
    }
    assert digests == {snapshot["stage1"]["sha256"], snapshot["stage2"]["sha256"]}


def test_reopen_stage_matches_the_failed_stage(tmp_path: Path) -> None:
    """阶段一失败的行重开阶段一；阶段二失败的行重开阶段二。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)
    probe.do(
        [
            ("UPDATE jobs SET status = 'completed' WHERE job_id = ?", (created.job_id,)),
            (
                "UPDATE records SET status = 'failed', failure_stage = 'stage1',"
                " failure_json = '{\"code\": \"OUTPUT_INVALID\"}' WHERE order_index = 0",
                (),
            ),
            (
                "UPDATE records SET status = 'failed', failure_stage = 'stage2',"
                " failure_json = '{\"code\": \"OUTPUT_INVALID\"}' WHERE order_index = 1",
                (),
            ),
        ]
    )

    planned = plan(config, database, created.run_id, retry_failed=True)

    keys = [
        row["record_key"]
        for row in probe.all("SELECT record_key FROM records ORDER BY order_index")
    ]
    assert dict(planned.reopen) == {keys[0]: 1, keys[1]: 2}
    assert set(planned.selected_records) == set(keys)
