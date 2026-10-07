"""判别执行：**持久化**批次的单条执行（S04-03 起）。

- `SqliteBudgetLedger`/`execute_record`：调用前在短事务里占位 `call_attempts`，
  成功后同事务提交 `stage_results`、记录投影与 `runs.revision`，由 worker（S04-04）
  逐条驱动。

S03-07 的 `JudgeJob`/`JudgeJobRegistry`（`POST /api/judge` 的内存态临时路径）**已在
S06 删除**：判一条由整批 + 批次记录详情取代（S06 阶段文档 §0.2 第 1 项），任务状态
不再驻留 API 进程内存。诊断产物的写入随之搬到 `agent/diagnostics.py`，由 worker 的
记录失败路径调用。

为什么不在请求里同步跑完：单条真实判别最长可达阶段一 120 秒 + 阶段二 300 秒，
[plan/02 §5](../../../plan/02-架构与详细设计.md) 明确“页面轮询任务状态，不维持一条
数分钟的模型请求连接”。因此由独立 worker 逐条执行 + 前端轮询批次状态。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..agent.pipeline import (
    AttemptCompletion,
    RecordResult,
    run_single,
)
from ..config import AppConfig
from ..llm.client import STAGE1, STAGE2, ModelClient
from ..repositories import records as records_repo
from ..repositories import runs as runs_repo
from ..schemas.analysis import STAGE1_SCHEMA_VERSION, Stage1Analysis
from ..storage import Database, utc_now, write_transaction
from .batches import BatchError

# ------------------------------------------------ S04-03：持久化预算与阶段提交


class LedgerStateError(RuntimeError):
    """账本与已提交数据不一致；**停止执行并报告**，不猜测修复。"""


@dataclass(frozen=True)
class RecordExecution:
    """一次记录执行：开始时的检查点与最终结果。"""

    record_key: str
    checkpoint: str
    result: RecordResult


def _usage_json(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


class SqliteBudgetLedger:
    """把阶段预算与阶段提交落到 SQLite 的账本（S04-03）。

    实现 `agent.pipeline.BudgetLedger`：`record_attempt` 在**调用模型之前**用一条
    短事务写入 `running` 占位，`complete_attempt` 在返回后用另一条短事务写终态；
    模型请求本身**从不**发生在事务里（[.claude/rules/storage.md]）。

    **每次操作各开一条独立连接**（用户 2026-10-06 决定）：进程被杀时不会有半开
    事务残留，SQLite 的写锁只在每次极短的提交期间持有。

    预算不退还：占位行一旦提交就计入已用次数，包括失败、超时与中断未知的尝试。
    """

    def __init__(
        self,
        database: Database,
        *,
        record_key: str,
        run_id: str,
        job_id: str | None,
        max_attempts: int,
        prompt_sha256: Mapping[str, str],
        requested_model: Mapping[str, str | None] | None = None,
        reason: str = records_repo.CAMPAIGN_INITIAL,
    ) -> None:
        self._database = database
        self._record_key = record_key
        self._run_id = run_id
        self._job_id = job_id
        self._max_attempts = max_attempts
        self._prompt_sha256 = dict(prompt_sha256)
        self._requested_model = dict(requested_model or {})
        self._reason = reason

    # ------------------------------------------------------------ 协议实现

    def attempts_used(self, record_key: str, stage: str) -> int:
        self._check_key(record_key)
        number = records_repo.stage_number(stage)
        connection = self._database.connect()
        try:
            campaign = records_repo.latest_campaign(connection, self._record_key, number)
            if campaign is None:
                return 0
            return records_repo.count_campaign_attempts(connection, campaign.campaign_id)
        finally:
            connection.close()

    def record_attempt(self, record_key: str, stage: str) -> int:
        self._check_key(record_key)
        number = records_repo.stage_number(stage)
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                campaign = records_repo.latest_campaign(tx, self._record_key, number)
                if campaign is None:
                    campaign = records_repo.open_campaign(
                        tx,
                        record_key=self._record_key,
                        stage=number,
                        max_attempts=self._max_attempts,
                        reason=self._reason,
                        created_at=utc_now(),
                        job_id=self._job_id,
                    )
                attempt_no = (
                    records_repo.count_campaign_attempts(tx, campaign.campaign_id) + 1
                )
                records_repo.reserve_attempt(
                    tx,
                    campaign_id=campaign.campaign_id,
                    attempt_no=attempt_no,
                    started_at=utc_now(),
                    job_id=self._job_id,
                    requested_model_id=self._requested_model.get(stage),
                )
                return attempt_no
        finally:
            connection.close()

    def complete_attempt(
        self, record_key: str, stage: str, attempt_no: int, completion: AttemptCompletion
    ) -> None:
        """写尝试终态；成功时**同一事务**提交阶段结果、记录投影与批次 revision。"""
        self._check_key(record_key)
        number = records_repo.stage_number(stage)
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                campaign = records_repo.latest_campaign(tx, self._record_key, number)
                if campaign is None:
                    raise LedgerStateError(
                        f"记录 {self._record_key} 的阶段 {number} 没有预算轮次，却有尝试回报"
                    )
                attempt = records_repo.attempt_by_no(
                    tx, campaign.campaign_id, attempt_no
                )
                if attempt is None:
                    raise LedgerStateError(
                        f"记录 {self._record_key} 的阶段 {number} 第 {attempt_no} 次尝试没有占位行"
                    )
                if attempt.status != records_repo.ATTEMPT_RUNNING:
                    # 已经回报过：不重复写终态，也不重复插阶段结果。
                    return

                now = utc_now()
                if completion.outcome == "ok":
                    self._commit_stage(tx, number, attempt.attempt_id, completion, now)
                else:
                    records_repo.finish_attempt(
                        tx,
                        attempt_id=attempt.attempt_id,
                        status=records_repo.ATTEMPT_FAILED,
                        finished_at=now,
                        returned_model_id=completion.model,
                        usage=_usage_json(completion.usage),
                        duration_ms=completion.latency_ms,
                        error={
                            "code": completion.error_code,
                            "message": completion.error_message,
                        },
                        content=completion.content,
                        simulated=completion.simulated,
                    )
        finally:
            connection.close()

    def reset(self, record_key: str, stage: str) -> None:
        """显式重开一轮预算：新 campaign，`campaign_no` 加一，上限不变。

        普通恢复**不**调用本方法（[plan/10 §6]）；没有它的调用方就只能用掉原有
        轮次的剩余次数。
        """
        self._check_key(record_key)
        number = records_repo.stage_number(stage)
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                records_repo.open_campaign(
                    tx,
                    record_key=self._record_key,
                    stage=number,
                    max_attempts=self._max_attempts,
                    reason=records_repo.CAMPAIGN_EXPLICIT_RETRY,
                    created_at=utc_now(),
                    job_id=self._job_id,
                )
        finally:
            connection.close()

    # ---------------------------------------------------------------- 内部

    def _commit_stage(
        self,
        connection: sqlite3.Connection,
        number: int,
        attempt_id: str,
        completion: AttemptCompletion,
        now: str,
    ) -> None:
        """成功尝试 + 阶段结果 + 记录投影 + 批次 revision，一次提交。"""
        if completion.result_json is None or completion.schema_version is None:
            raise LedgerStateError("成功尝试缺少已校验的阶段结果，拒绝半次提交")
        prompt_sha256 = self._prompt_sha256.get(
            records_repo.stage_name(number), ""
        )
        if not prompt_sha256:
            raise LedgerStateError(f"批次 {self._run_id} 缺少阶段 {number} 的提示词摘要")

        records_repo.finish_attempt(
            connection,
            attempt_id=attempt_id,
            status=records_repo.ATTEMPT_SUCCEEDED,
            finished_at=now,
            returned_model_id=completion.model,
            usage=_usage_json(completion.usage),
            duration_ms=completion.latency_ms,
            content=completion.content,
            simulated=completion.simulated,
        )
        records_repo.insert_stage_result(
            connection,
            record_key=self._record_key,
            stage=number,
            attempt_id=attempt_id,
            result_json=completion.result_json,
            schema_version=completion.schema_version,
            prompt_sha256=prompt_sha256,
            validated_at=now,
        )
        if number == 1:
            records_repo.mark_stage1_done(connection, self._record_key, updated_at=now)
        else:
            if completion.final_label is None or completion.review_required is None:
                raise LedgerStateError("阶段二成功但没有标签或复核标记，拒绝写入完成状态")
            records_repo.mark_completed(
                connection,
                self._record_key,
                final_label=completion.final_label,
                review_required=completion.review_required,
                updated_at=now,
            )
        runs_repo.bump_revision(connection, self._run_id)

    def _check_key(self, record_key: str) -> None:
        if record_key != self._record_key:
            raise LedgerStateError(
                f"账本绑定记录 {self._record_key}，收到 {record_key!r}；拒绝误记预算"
            )


def execute_record(
    database: Database,
    config: AppConfig,
    *,
    run_id: str,
    record_key: str,
    job_id: str | None,
    client: ModelClient,
    sleep: Callable[[float], None] = time.sleep,
) -> RecordExecution:
    """执行一条**已落库**的记录：从已提交的检查点开始，逐阶段提交。

    阶段一已提交的记录直接进入阶段二，不重跑、不占用阶段一预算；提示词一律取批次
    快照，不读磁盘当前版本。失败行在开始执行时按检查点重置（[plan/10 §3]），当前
    失败与预算落实分开提交：预算在调用前占位，失败投影在阶段确定失败后写入。
    """
    connection = database.connect()
    try:
        run = runs_repo.get_run(connection, run_id)
        record = records_repo.get_record(connection, record_key)
        snapshot = runs_repo.get_prompt_snapshot(connection, run_id)
        stored_stage1 = records_repo.get_stage_result(connection, record_key, 1)
    finally:
        connection.close()

    if run is None:
        raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)
    if record is None or record.run_id != run_id:
        raise BatchError(
            "NOT_FOUND",
            f"批次 {run_id} 中没有记录 {record_key}",
            http_status=404,
        )
    if record.status == "input_invalid":
        raise BatchError(
            "STATE_CONFLICT",
            f"记录 {record_key} 是输入失败行，不调用模型",
            details={"status": record.status},
        )
    if record.status == "completed":
        raise BatchError(
            "STATE_CONFLICT",
            f"记录 {record_key} 已完成；普通恢复不重复调用模型",
            details={"status": record.status},
        )

    prompt_text: dict[str, str] = {}
    prompt_sha256: dict[str, str] = {}
    for stage in (STAGE1, STAGE2):
        entry = snapshot.get(stage) or {}
        text = entry.get("text")
        digest = entry.get("sha256")
        if not text or not digest:
            raise BatchError(
                "VERSION_INCOMPATIBLE",
                f"批次 {run_id} 的提示词快照缺少 {stage} 正文；请新建批次",
                details={"run_id": run_id, "stage": stage},
            )
        prompt_text[stage] = text
        prompt_sha256[stage] = digest

    stage1_result: Stage1Analysis | None = None
    checkpoint = "pending"
    if stored_stage1 is not None:
        if (
            stored_stage1.schema_version != STAGE1_SCHEMA_VERSION
            or stored_stage1.prompt_sha256 != prompt_sha256[STAGE1]
        ):
            raise BatchError(
                "VERSION_INCOMPATIBLE",
                "已提交的阶段一结果与当前批次的版本不一致；请新建批次",
                details={"record_key": record_key},
            )
        stage1_result = Stage1Analysis.model_validate_json(stored_stage1.result_json)
        checkpoint = "stage1_done"

    if record.status != checkpoint:
        _reset_to_checkpoint(database, run_id, record_key, checkpoint)

    ledger = SqliteBudgetLedger(
        database,
        record_key=record_key,
        run_id=run_id,
        job_id=job_id,
        max_attempts=config.execution.max_attempts_per_stage_campaign,
        prompt_sha256=prompt_sha256,
        requested_model={STAGE1: config.stage1.model, STAGE2: config.stage2.model},
    )
    result = run_single(
        record.to_qa_record(),
        client,
        execution=config.execution,
        budget=ledger,
        record_key=record_key,
        prompts=prompt_text,
        stage1=stage1_result,
        sleep=sleep,
    )
    if result.status == "failed":
        _commit_record_failure(database, run_id, record_key, result)
    return RecordExecution(record_key=record_key, checkpoint=checkpoint, result=result)


def _reset_to_checkpoint(
    database: Database, run_id: str, record_key: str, checkpoint: str
) -> None:
    """把可重试行按已提交的阶段结果重置回检查点状态。"""
    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            records_repo.set_checkpoint_status(
                tx, record_key, checkpoint, updated_at=utc_now()
            )
            runs_repo.bump_revision(tx, run_id)
    finally:
        connection.close()


def _commit_record_failure(
    database: Database, run_id: str, record_key: str, result: RecordResult
) -> None:
    """记录级技术失败：不给标签，只留受控错误与失败阶段。"""
    if result.failure is None:  # pragma: no cover - failed 必有 failure
        raise LedgerStateError(f"记录 {record_key} 状态为 failed 但没有失败详情")
    failure = result.failure
    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            records_repo.mark_failed(
                tx,
                record_key,
                failure_stage=records_repo.stage_number(failure.stage),
                failure={
                    "code": failure.code,
                    "message": failure.message,
                    "attempt_count": failure.attempt_count,
                    "retryable": failure.retryable,
                },
                updated_at=utc_now(),
            )
            runs_repo.bump_revision(tx, run_id)
    finally:
        connection.close()


__all__ = [
    "LedgerStateError",
    "RecordExecution",
    "STAGE1",
    "STAGE2",
    "SqliteBudgetLedger",
    "execute_record",
]
