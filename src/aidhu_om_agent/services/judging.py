"""判别执行：S03 的**内存态**判一条，与 S04 的**持久化**单条执行。

两部分的边界要分清：

- `JudgeJob`/`JudgeJobRegistry`（S03-07）：`POST /api/judge` 的临时路径，任务状态
  只在 API 进程内存里，**不承诺中断恢复**，S06 由批次接口取代。
- `SqliteBudgetLedger`/`execute_record`（S04-03）：持久化批次的记录执行——调用前
  在短事务里占位 `call_attempts`，成功后同事务提交 `stage_results`、记录投影与
  `runs.revision`，由 worker（S04-04）逐条驱动。

S03-07 那部分（`JudgeJob`/`JudgeJobRegistry`）是**临时实现，必须显式标注**：任务状态
只存在 API 进程的内存里，**进程重启即丢失，本阶段不承诺中断恢复**。任务状态的形状
刻意贴近 [plan/08 §7](../../../plan/08-API接口与数据合同.md) 的 job 语义，S06 换成批次
接口时前端不用改。

为什么不在请求里同步跑完：单条真实判别最长可达阶段一 120 秒 + 阶段二 300 秒，
[plan/02 §5](../../../plan/02-架构与详细设计.md) 明确“页面轮询任务状态，不维持一条
数分钟的模型请求连接”。因此改为后台线程执行 + 前端轮询。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..agent.diagnostics import diagnose_raw_output
from ..agent.mock_samples import MockClient
from ..agent.pipeline import (
    AttemptCompletion,
    BudgetLedger,
    InMemoryBudgetLedger,
    RecordResult,
    result_to_payload,
    run_single,
)
from ..agent.stage1 import STAGE1_PROMPT_VERSION
from ..agent.stage2 import STAGE2_PROMPT_VERSION
from ..config import AppConfig
from ..llm.client import STAGE1, STAGE2, ModelClient, ModelResponse
from ..repositories import records as records_repo
from ..repositories import runs as runs_repo
from ..schemas.analysis import STAGE1_SCHEMA_VERSION, Stage1Analysis
from ..schemas.judgement import STAGE2_SCHEMA_VERSION
from ..storage import Database, utc_now, write_transaction
from .batches import BatchError

#: 任务状态；与 plan/08 §7 的 job 状态同名，本阶段实际只会出现前四者。
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
PARTIAL_FAILED = "partial_failed"
FAILED = "failed"


def _now_iso() -> str:
    """本地时区的 ISO 8601 时间戳（含偏移），供任务时间字段使用。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class _ReportingClient:
    """包装客户端，在每次阶段调用**之前**回报当前阶段与本次尝试序号。

    **边界**：这里的计数只在「账本从空开始」时等于阶段尝试序号，因此只用于
    `/api/judge` 这条内存态临时路径（每次判别都新建内存账本）。持久化批次改由
    `SqliteBudgetLedger` 从 `call_attempts` 读尝试序号，恢复后不会从 1 重新计数。
    """

    def __init__(
        self, inner: ModelClient, on_call: Callable[[str, int], None]
    ) -> None:
        self._inner = inner
        self._on_call = on_call
        self._counts: dict[str, int] = {}

    def call(self, messages: Sequence[Mapping[str, str]], stage: str) -> ModelResponse:
        self._counts[stage] = self._counts.get(stage, 0) + 1
        self._on_call(stage, self._counts[stage])
        return self._inner.call(messages, stage)


@dataclass
class JudgeJob:
    """一次判一条任务的内存记录。"""

    job_id: str
    validation_id: str
    record_key: str
    mode: str
    kind: str = "classify"
    status: str = QUEUED
    current_stage: str | None = None
    current_attempt: int | None = None
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _started_monotonic: float | None = field(default=None, repr=False)

    def begin(self) -> None:
        """进入 running，并记下开始时间（用于展示已用时长）。"""
        with self._lock:
            self.status = RUNNING
            self.started_at = _now_iso()
            self._started_monotonic = time.monotonic()

    def set_stage(self, stage: str, attempt: int) -> None:
        with self._lock:
            self.current_stage = stage
            self.current_attempt = attempt

    def finish(
        self,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """进入终态；成功与失败都要落 ``finished_at``，不留悬挂的 running。"""
        with self._lock:
            self.status = status
            self.current_stage = None
            self.current_attempt = None
            self.finished_at = _now_iso()
            if self._started_monotonic is not None:
                self.elapsed_ms = int((time.monotonic() - self._started_monotonic) * 1000)
            if result is not None:
                self.result = result
            if error is not None:
                self.error = error

    def payload(self) -> dict[str, Any]:
        """可直接返回前端的任务视图；不包含模型推理内容与调试堆栈。

        ``created_at``/``started_at``/``finished_at`` 对齐
        [plan/08 §7](../../../plan/08-API接口与数据合同.md) 的任务字段；
        ``elapsed_ms``/``current_attempt`` 是本阶段为「四分钟没动静」这类体验问题
        增加的内部扩展，最终契约由 S06 定。
        """
        with self._lock:
            return {
                "job_id": self.job_id,
                "kind": self.kind,
                "mode": self.mode,
                "status": self.status,
                "current_stage": self.current_stage,
                "current_attempt": self.current_attempt,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_ms": self.elapsed_ms,
                "validation_id": self.validation_id,
                "record_key": self.record_key,
                "result": self.result,
                "error": self.error,
            }


class JudgeJobRegistry:
    """内存态任务表 + 后台线程执行。

    ``submit`` 立即返回；执行在 daemon 线程中进行，因此进程退出不会挂住。
    **不承诺中断恢复**：进程重启后任务与结果全部丢失（S04 解决）。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._jobs: dict[str, JudgeJob] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        record: Any,
        validation_id: str,
        record_key: str,
        mode: str,
    ) -> JudgeJob:
        job = JudgeJob(
            job_id=uuid.uuid4().hex,
            validation_id=validation_id,
            record_key=record_key,
            mode=mode,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        threading.Thread(
            target=self._execute, args=(job, record), name=f"judge-{job.job_id}", daemon=True
        ).start()
        return job

    def get(self, job_id: str) -> JudgeJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    # ------------------------------------------------------------------ 执行

    def _build_client(self, job: JudgeJob, record: Any) -> ModelClient:
        if job.mode == "mock":
            return MockClient(record)
        # 真实客户端延迟构造：缺少服务地址或凭据时在首次调用处失败，
        # 由 run_single 归类为 CONFIG_INVALID 并写入任务 error。
        from ..llm.client import OpenAICompatibleClient

        return OpenAICompatibleClient(self._config)

    def _execute(self, job: JudgeJob, record: Any) -> None:
        job.begin()

        client = _ReportingClient(self._build_client(job, record), job.set_stage)
        budget: BudgetLedger = InMemoryBudgetLedger()
        try:
            result = run_single(
                record,
                client,
                execution=self._config.execution,
                budget=budget,
            )
        except Exception as exc:  # 未预期异常也要落到任务上，不留 running 悬挂
            job.finish(
                FAILED,
                error={
                    "code": "INTERNAL_ERROR",
                    "message": f"判别任务异常终止：{type(exc).__name__}",
                },
            )
            return

        payload = result_to_payload(result)
        job.finish(
            COMPLETED if result.status == "completed" else PARTIAL_FAILED,
            result=payload,
        )
        self._write_diagnostic(job, record, payload)

    # -------------------------------------------------------------- 诊断产物

    def _write_diagnostic(self, job: JudgeJob, record: Any, payload: dict[str, Any]) -> None:
        """把失败/被拒输出的判读材料写到 runtime 目录。

        **整段包 try/except**：诊断是观测手段，写盘失败绝不能改变任务状态，
        也不能把已完成的任务标成失败。
        """
        try:
            attempts = payload.get("attempts") or []
            if payload.get("status") == "completed" and not any(
                attempt.get("outcome") != "ok" for attempt in attempts
            ):
                return

            refs = getattr(record, "refs", None) or {}
            diagnosed = []
            for attempt in attempts:
                raw_output = attempt.get("raw_output")
                diagnosed.append(
                    {
                        "stage": attempt.get("stage"),
                        "attempt": attempt.get("attempt"),
                        "outcome": attempt.get("outcome"),
                        "latency_ms": attempt.get("latency_ms"),
                        "error_code": attempt.get("error_code"),
                        "error_message": attempt.get("error_message"),
                        "raw_output_truncated": attempt.get("raw_output_truncated"),
                        "raw_output": raw_output,
                        "quote_diagnosis": (
                            diagnose_raw_output(raw_output, refs) if raw_output else None
                        ),
                    }
                )

            document = {
                "job_id": job.job_id,
                "validation_id": job.validation_id,
                "record_key": job.record_key,
                "record_id": payload.get("record_id"),
                "source_row": payload.get("source_row"),
                "mode": job.mode,
                # 任务状态与记录状态分开写：前者可能是 partial_failed，后者只有
                # completed/failed，混用会让人分不清是哪一层的结果。
                "job_status": job.status,
                "record_status": payload.get("status"),
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "elapsed_ms": job.elapsed_ms,
                "models": sorted(
                    {
                        attempt.get("model")
                        for attempt in attempts
                        if attempt.get("model")
                    }
                ),
                "prompt_versions": {
                    "stage1": STAGE1_PROMPT_VERSION,
                    "stage2": STAGE2_PROMPT_VERSION,
                },
                "schema_versions": {
                    "stage1": STAGE1_SCHEMA_VERSION,
                    "stage2": STAGE2_SCHEMA_VERSION,
                },
                "failure": payload.get("failure"),
                "error": job.error,
                "attempts": diagnosed,
            }

            directory = self._config.paths.runtime / "judge-diagnostics"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"judge-{job.job_id}.json"
            target.write_text(
                json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - 观测失败不得影响判定结果
            return


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
    "COMPLETED",
    "FAILED",
    "PARTIAL_FAILED",
    "QUEUED",
    "RUNNING",
    "JudgeJob",
    "JudgeJobRegistry",
    "LedgerStateError",
    "RecordExecution",
    "STAGE1",
    "STAGE2",
    "SqliteBudgetLedger",
    "execute_record",
]
