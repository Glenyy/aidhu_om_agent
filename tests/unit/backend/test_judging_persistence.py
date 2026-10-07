"""S04-03 持久化阶段预算与提交的单测。

覆盖 [plan/09 §“保存阶段”] 与 [plan/10 §3—§5] 的关键不变量：

- **占位先于调用**：`call_attempts` 的 `running` 行在模型请求**之前**已提交；
  调用期间不持有写事务（另一条连接仍能写）。
- **一次提交**：成功尝试 + `stage_results` + 记录状态/投影 + `runs.revision`
  同一事务，不出现「阶段一已保存但记录仍 pending」。
- **失败不填标签**：失败保留尝试与受控错误，阶段二失败**保留阶段一结果**。
- **预算不退还**：中断遗留的 `running` 占位照样占预算；预算从数据库读，不从
  内存读；用尽后不再调用模型。
- **恢复走检查点与快照**：阶段一已提交则直接进阶段二，提示词取批次快照。

全部使用合成工作簿、模拟客户端与本地 SQLite，**零真实模型调用**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_batches import make_config, prepare_validation, read_rows

from aidhu_om_agent.agent.pipeline import AttemptCompletion
from aidhu_om_agent.agent.mock_samples import SCENARIO_FORCED_FAILURE, MockClient
from aidhu_om_agent.config import ExecutionConfig
from aidhu_om_agent.llm.client import STAGE1, STAGE2, ModelResponse
from aidhu_om_agent.llm.errors import PermanentModelError
from aidhu_om_agent.repositories import records as records_repo
from aidhu_om_agent.services.batches import BatchError, create_run
from aidhu_om_agent.services.judging import (
    LedgerStateError,
    SqliteBudgetLedger,
    execute_record,
)
from aidhu_om_agent.storage import Database, write_transaction
from aidhu_om_agent.version import prompt_fingerprint
from fixtures.excel_samples import normal_workbook, partial_failure_workbook

PROBE_TIMEOUT_MS = 250


class ProbingClient:
    """包装模拟客户端，在每次**调用之前**执行探针（用于核对占位与锁）。"""

    def __init__(self, inner: MockClient, on_call=None) -> None:
        self._inner = inner
        self._on_call = on_call
        self.stages: list[str] = []

    def call(self, messages, stage: str) -> ModelResponse:
        if self._on_call is not None:
            self._on_call(stage)
        self.stages.append(stage)
        return self._inner.call(messages, stage)

    @property
    def calls(self) -> list[tuple[str, list[dict[str, str]]]]:
        return self._inner.calls


def prepare_run(tmp_path: Path, *, max_attempts: int | None = None):
    """建一个含 3 条有效记录的批次，返回 (config, database, run_id, records)。"""
    config = make_config(tmp_path)
    if max_attempts is not None:
        config = replace(
            config,
            execution=ExecutionConfig(
                concurrency=1,
                max_attempts_per_stage_campaign=max_attempts,
                retry_backoff_seconds=(0, 0),
            ),
        )
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(database, config, validation_id=validation_id)
    connection = database.connect()
    try:
        rows = records_repo.list_records(connection, created.run_id)
    finally:
        connection.close()
    return config, database, created.run_id, created.job_id, rows


def run_record(config, database, run_id, job_id, row, client):
    return execute_record(
        database,
        config,
        run_id=run_id,
        record_key=row.record_key,
        job_id=job_id,
        client=client,
        sleep=lambda _seconds: None,
    )


def load_record(database: Database, record_key: str) -> records_repo.RecordRow:
    connection = database.connect()
    try:
        row = records_repo.get_record(connection, record_key)
    finally:
        connection.close()
    assert row is not None
    return row


# ------------------------------------------------------- 占位先于调用、不占写锁


def test_attempt_is_reserved_before_the_model_call(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    seen: list[tuple[str, list[tuple[int, str]]]] = []

    def probe(stage: str) -> None:
        seen.append((stage, [(r["attempt_no"], r["status"]) for r in read_rows(
            database,
            "SELECT a.attempt_no, a.status FROM call_attempts a JOIN stage_campaigns c"
            " ON a.campaign_id = c.campaign_id WHERE c.record_key = ? ORDER BY a.started_at",
            (row.record_key,),
        )]))

    client = ProbingClient(MockClient(row.to_qa_record(), scenario="correct"), probe)
    execution = run_record(config, database, run_id, job_id, row, client)

    assert execution.result.status == "completed"
    # 第一次调用时已有 1 行 running；第二次调用时第一行已终结、第二行正在运行。
    assert seen[0][1] == [(1, "running")]
    assert seen[1][1] == [(1, "succeeded"), (1, "running")]


def test_model_call_does_not_hold_a_write_lock(tmp_path: Path) -> None:
    """模型请求不在事务里：调用期间另一条连接能立刻写入。"""
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    other = Database(config.paths.database, busy_timeout_ms=PROBE_TIMEOUT_MS)
    writes: list[int] = []

    def probe(stage: str) -> None:
        connection = other.connect()
        try:
            with write_transaction(connection) as tx:
                tx.execute(
                    "UPDATE runs SET last_error_json = NULL WHERE run_id = ?", (run_id,)
                )
                writes.append(1)
        finally:
            connection.close()

    client = ProbingClient(MockClient(row.to_qa_record(), scenario="correct"), probe)
    run_record(config, database, run_id, job_id, row, client)

    assert writes == [1, 1]


# -------------------------------------------------------- 成功：一次提交四个产物


def test_success_commits_results_projection_and_revision(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    client = MockClient(row.to_qa_record(), scenario="correct")

    execution = run_record(config, database, run_id, job_id, row, client)
    result = execution.result

    assert execution.checkpoint == "pending"
    assert result.status == "completed" and result.stage2 is not None

    stored = load_record(database, row.record_key)
    assert stored.status == "completed"
    assert stored.final_label == result.stage2.label.value
    assert stored.review_required == (1 if result.stage2.review_required else 0)
    assert stored.failure_stage is None and stored.failure is None

    results = read_rows(
        database,
        "SELECT * FROM stage_results WHERE record_key = ? ORDER BY stage",
        (row.record_key,),
    )
    assert [item["stage"] for item in results] == [1, 2]
    assert [item["schema_version"] for item in results] == ["1.1", "1.1"]
    assert all(len(item["prompt_sha256"]) == 64 for item in results)
    # result_json 是规范化文本，落库摘要与其逐字节一致。
    for item in results:
        assert item["result_sha256"] == hashlib.sha256(
            item["result_json"].encode("utf-8")
        ).hexdigest()

    (run,) = read_rows(database, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    # 两次阶段提交各递增一次；其余两条记录仍是 pending，不贡献 revision。
    assert run["revision"] == 2
    counts = records_repo.count_by_status(database.connect(), run_id)
    assert counts["completed"] == 1 and counts["pending"] == 2

    attempts = read_rows(
        database,
        "SELECT a.* FROM call_attempts a JOIN stage_campaigns c"
        " ON a.campaign_id = c.campaign_id WHERE c.record_key = ? ORDER BY c.stage",
        (row.record_key,),
    )
    # 尝试行数 == 实际调用次数：SDK/执行器/worker 不叠加放大预算。
    assert len(attempts) == len(client.calls) == 2
    assert [item["status"] for item in attempts] == ["succeeded", "succeeded"]
    assert all(item["attempt_no"] == 1 for item in attempts)
    assert attempts[0]["requested_model_id"] == config.stage1.model
    assert attempts[0]["returned_model_id"] == "mock-deterministic"
    assert attempts[0]["duration_ms"] == 0


def test_completed_record_is_not_executed_again(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    run_record(
        config, database, run_id, job_id, row, MockClient(row.to_qa_record(), "correct")
    )

    client = MockClient(row.to_qa_record(), "correct")
    with pytest.raises(BatchError) as error:
        run_record(config, database, run_id, job_id, row, client)

    assert error.value.code == "STATE_CONFLICT"
    assert client.calls == []


def test_input_invalid_record_is_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = partial_failure_workbook(tmp_path / "src" / "partial.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(database, config, validation_id=validation_id)
    connection = database.connect()
    try:
        rows = records_repo.list_records(connection, created.run_id)
    finally:
        connection.close()
    invalid = next(row for row in rows if row.status == "input_invalid")

    with pytest.raises(BatchError) as error:
        run_record(
            config, database, created.run_id, created.job_id, invalid, MockClient(
                invalid.to_qa_record(), "correct"
            )
        )

    assert error.value.code == "STATE_CONFLICT"


# ------------------------------------------------------------------ 失败路径


def test_validation_failure_keeps_attempts_without_a_label(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    client = MockClient(row.to_qa_record(), scenario=SCENARIO_FORCED_FAILURE)

    execution = run_record(config, database, run_id, job_id, row, client)
    result = execution.result

    assert result.status == "failed"
    assert result.failure is not None and result.failure.stage == STAGE2
    assert result.failure.code == "OUTPUT_INVALID" and result.failure.attempt_count == 3
    # 阶段一确实通过了，它的结果被保留（plan/10 §3）。
    assert result.stage1 is not None

    stored = load_record(database, row.record_key)
    assert stored.status == "failed"
    assert stored.final_label is None and stored.review_required is None
    assert stored.failure_stage == "stage2"
    assert stored.failure == {
        "code": "OUTPUT_INVALID",
        "message": stored.failure["message"],
        "attempt_count": 3,
        "retryable": False,
    }

    attempts = read_rows(
        database,
        "SELECT a.* FROM call_attempts a JOIN stage_campaigns c"
        " ON a.campaign_id = c.campaign_id WHERE c.record_key = ? ORDER BY c.stage, a.attempt_no",
        (row.record_key,),
    )
    assert len(attempts) == 4 == len(client.calls)
    assert (attempts[0]["status"], attempts[0]["attempt_no"]) == ("succeeded", 1)
    assert [item["status"] for item in attempts[1:]] == ["failed"] * 3
    assert [item["attempt_no"] for item in attempts[1:]] == [1, 2, 3]
    # 被拒输出按 S03 返工 R-1 留存，错误是受控码而不是异常堆栈。
    assert all(item["final_content"] for item in attempts[1:])
    assert json.loads(attempts[1]["error_json"])["code"] == "OUTPUT_INVALID"

    # 只有阶段一有已校验结果；没有阶段二结果，也就没有标签。
    results = read_rows(
        database,
        "SELECT stage FROM stage_results WHERE record_key = ? ORDER BY stage",
        (row.record_key,),
    )
    assert [item["stage"] for item in results] == [1]

    (run,) = read_rows(database, "SELECT revision FROM runs WHERE run_id = ?", (run_id,))
    assert run["revision"] == 2  # 阶段一提交 + 记录失败


def test_record_failure_records_controlled_error_for_model_errors(tmp_path: Path) -> None:
    class ConfigInvalidError(PermanentModelError):
        """凭据/配置类不可重试错误（plan/10 §4：不继续试满预算）。"""

        code = "CONFIG_INVALID"

    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]

    class FailingClient:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, messages, stage):
            self.calls += 1
            raise ConfigInvalidError("认证失败")

    client = FailingClient()
    execution = run_record(config, database, run_id, job_id, row, client)

    assert execution.result.status == "failed"
    # 不可重试错误只调用一次，不把预算试满。
    assert client.calls == 1
    stored = load_record(database, row.record_key)
    assert stored.failure_stage == "stage1"
    assert stored.failure["code"] == "CONFIG_INVALID"
    assert stored.failure["attempt_count"] == 1
    assert read_rows(
        database, "SELECT stage FROM stage_results WHERE record_key = ?", (row.record_key,)
    ) == []
    (attempt,) = read_rows(database, "SELECT status, error_json FROM call_attempts")
    assert attempt["status"] == "failed"
    assert json.loads(attempt["error_json"])["code"] == "CONFIG_INVALID"


# ---------------------------------------------------------------------- 预算


def test_budget_is_read_from_the_database(tmp_path: Path) -> None:
    """新账本实例仍看得到已占用的次数：预算不靠进程内存。"""
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    run_record(
        config,
        database,
        run_id,
        job_id,
        row,
        MockClient(row.to_qa_record(), SCENARIO_FORCED_FAILURE),
    )

    connection = database.connect()
    try:
        campaign = records_repo.latest_campaign(connection, row.record_key, 2)
        assert campaign is not None
        assert (campaign.campaign_no, campaign.reason) == (1, "initial")
        assert records_repo.count_campaign_attempts(connection, campaign.campaign_id) == 3
    finally:
        connection.close()

    fresh = SqliteBudgetLedger(
        database,
        record_key=row.record_key,
        run_id=run_id,
        job_id=job_id,
        max_attempts=config.execution.max_attempts_per_stage_campaign,
        prompt_sha256={STAGE1: "0" * 64, STAGE2: "0" * 64},
    )
    assert fresh.attempts_used(row.record_key, STAGE2) == 3
    assert fresh.attempts_used(row.record_key, STAGE1) == 1
    with pytest.raises(LedgerStateError):
        fresh.attempts_used("another-record", STAGE2)


def test_interrupted_running_attempt_still_consumes_budget(tmp_path: Path) -> None:
    """占位已提交、结果未回：预算照样扣掉，恢复不会白送一次调用。"""
    config, database, run_id, job_id, rows = prepare_run(tmp_path, max_attempts=1)
    row = rows[0]
    ledger = SqliteBudgetLedger(
        database,
        record_key=row.record_key,
        run_id=run_id,
        job_id=job_id,
        max_attempts=1,
        prompt_sha256={STAGE1: "0" * 64, STAGE2: "0" * 64},
    )
    assert ledger.record_attempt(row.record_key, STAGE1) == 1
    # 此处不调用 complete_attempt：模拟进程在调用中被杀，行留在 running。

    client = MockClient(row.to_qa_record(), scenario="correct")
    execution = run_record(config, database, run_id, job_id, row, client)

    assert execution.result.status == "failed"
    assert execution.result.failure.code == "BUDGET_EXHAUSTED"
    assert client.calls == []
    # 那一行仍是 running（S04-04 的启动恢复才把它改成 unknown_after_interrupt），
    # 但它已经占掉唯一一次预算，所以没有再发生调用。
    assert [
        item["status"] for item in read_rows(database, "SELECT status FROM call_attempts")
    ] == ["running"]
    assert [
        item["stage"] for item in read_rows(database, "SELECT stage FROM stage_campaigns")
    ] == [1]


def test_no_new_campaign_is_opened_by_ordinary_recovery(tmp_path: Path) -> None:
    """预算耗尽后普通恢复不再调用模型，也不偷偷重开一轮。"""
    config, database, run_id, job_id, rows = prepare_run(tmp_path, max_attempts=1)
    row = rows[0]
    run_record(
        config,
        database,
        run_id,
        job_id,
        row,
        MockClient(row.to_qa_record(), SCENARIO_FORCED_FAILURE),
    )

    client = MockClient(row.to_qa_record(), "correct")
    execution = run_record(config, database, run_id, job_id, row, client)

    assert execution.result.status == "failed"
    assert execution.result.failure.code == "BUDGET_EXHAUSTED"
    assert client.calls == []
    campaigns = read_rows(
        database,
        "SELECT campaign_no, reason FROM stage_campaigns WHERE record_key = ?"
        " ORDER BY stage, campaign_no",
        (row.record_key,),
    )
    assert [(item["campaign_no"], item["reason"]) for item in campaigns] == [
        (1, "initial"),
        (1, "initial"),
    ]


def test_reset_opens_a_new_campaign_only_for_explicit_retry(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path, max_attempts=1)
    row = rows[0]
    ledger = SqliteBudgetLedger(
        database,
        record_key=row.record_key,
        run_id=run_id,
        job_id=job_id,
        max_attempts=1,
        prompt_sha256={STAGE1: "0" * 64, STAGE2: "0" * 64},
    )
    ledger.record_attempt(row.record_key, STAGE2)
    ledger.complete_attempt(
        row.record_key, STAGE2, 1, _failed_completion("模拟网络错误")
    )
    assert ledger.attempts_used(row.record_key, STAGE2) == 1

    ledger.reset(row.record_key, STAGE2)

    assert ledger.attempts_used(row.record_key, STAGE2) == 0
    rows_ = read_rows(
        database,
        "SELECT campaign_no, reason, max_attempts FROM stage_campaigns WHERE record_key = ?",
        (row.record_key,),
    )
    assert [(item["campaign_no"], item["reason"]) for item in rows_] == [
        (1, "initial"),
        (2, "explicit_retry"),
    ]
    assert all(item["max_attempts"] == 1 for item in rows_)


def _failed_completion(message: str) -> AttemptCompletion:
    return AttemptCompletion(
        outcome="model_error", error_code="UPSTREAM_ERROR", error_message=message
    )


def test_campaign_guards_reject_out_of_range_budgets(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    connection = database.connect()
    try:
        with pytest.raises(ValueError):
            records_repo.open_campaign(
                connection,
                record_key=rows[0].record_key,
                stage=1,
                max_attempts=4,
                reason="initial",
                created_at="2026-10-06T00:00:00+08:00",
            )
        with pytest.raises(ValueError):
            records_repo.open_campaign(
                connection,
                record_key=rows[0].record_key,
                stage=1,
                max_attempts=3,
                reason="重试",
                created_at="2026-10-06T00:00:00+08:00",
            )
    finally:
        connection.close()


# ------------------------------------------------------------------- 恢复


def test_resume_from_stage1_skips_stage1_and_keeps_its_result(tmp_path: Path) -> None:
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    run_record(
        config,
        database,
        run_id,
        job_id,
        row,
        MockClient(row.to_qa_record(), SCENARIO_FORCED_FAILURE),
    )
    (before,) = read_rows(
        database,
        "SELECT stage_result_id, result_json, validated_at FROM stage_results"
        " WHERE record_key = ? AND stage = 1",
        (row.record_key,),
    )

    # 显式重开阶段二预算（普通恢复不重开；这里刻意验证重开后的执行路径）。
    SqliteBudgetLedger(
        database,
        record_key=row.record_key,
        run_id=run_id,
        job_id=job_id,
        max_attempts=config.execution.max_attempts_per_stage_campaign,
        prompt_sha256={STAGE1: "0" * 64, STAGE2: "0" * 64},
    ).reset(row.record_key, STAGE2)

    client = MockClient(row.to_qa_record(), scenario="correct")
    execution = run_record(config, database, run_id, job_id, row, client)

    assert execution.checkpoint == "stage1_done"
    assert execution.result.status == "completed"
    # 没有重跑阶段一：只发了阶段二的消息。
    assert [stage for stage, _ in client.calls] == [STAGE2]
    assert [
        item["stage"]
        for item in read_rows(
            database,
            "SELECT stage FROM stage_results WHERE record_key = ? ORDER BY stage",
            (row.record_key,),
        )
    ] == [1, 2]
    (after,) = read_rows(
        database,
        "SELECT stage_result_id, result_json, validated_at FROM stage_results"
        " WHERE record_key = ? AND stage = 1",
        (row.record_key,),
    )
    assert dict(after) == dict(before)

    attempts = read_rows(
        database,
        "SELECT c.stage, c.campaign_no, a.attempt_no FROM call_attempts a"
        " JOIN stage_campaigns c ON a.campaign_id = c.campaign_id"
        " WHERE c.record_key = ? ORDER BY c.stage, c.campaign_no, a.attempt_no",
        (row.record_key,),
    )
    # 阶段一是第 1 轮第 1 次；阶段二有第 1 轮的 3 次与第 2 轮的第 1 次。
    assert [(item["stage"], item["campaign_no"], item["attempt_no"]) for item in attempts] == [
        (1, 1, 1),
        (2, 1, 1),
        (2, 1, 2),
        (2, 1, 3),
        (2, 2, 1),
    ]


def test_execution_uses_the_frozen_prompt_snapshot(tmp_path: Path) -> None:
    """提示词取批次快照：磁盘内容改了也不影响已建批次。"""
    config, database, run_id, job_id, rows = prepare_run(tmp_path)
    row = rows[0]
    marker = "【快照标记】阶段一提示词已被替换，用于证明执行走的是批次快照。"

    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            snapshot = json.loads(
                tx.execute(
                    "SELECT prompt_snapshot_json FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()["prompt_snapshot_json"]
            )
            snapshot["stage1"]["text"] = marker
            snapshot["stage1"]["sha256"] = prompt_fingerprint(marker)
            tx.execute(
                "UPDATE runs SET prompt_snapshot_json = ? WHERE run_id = ?",
                (json.dumps(snapshot, ensure_ascii=False), run_id),
            )
    finally:
        connection.close()

    client = MockClient(row.to_qa_record(), scenario="correct")
    run_record(config, database, run_id, job_id, row, client)

    stage1_messages = [m for stage, ms in client.calls if stage == STAGE1 for m in ms]
    assert stage1_messages[0]["content"] == marker
    (stored,) = read_rows(
        database,
        "SELECT prompt_sha256 FROM stage_results WHERE record_key = ? AND stage = 1",
        (row.record_key,),
    )
    assert stored["prompt_sha256"] == prompt_fingerprint(marker)
