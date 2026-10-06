"""S03-07：判一条的**内存态**任务执行。

**临时实现，必须显式标注**：S03 尚无持久化与 worker（S04 才做），任务状态只存在
API 进程的内存里，**进程重启即丢失，本阶段不承诺中断恢复**。任务状态的形状刻意
贴近 [plan/08 §7](../../../plan/08-API接口与数据合同.md) 的 job 语义，S04 换成
SQLite 队列与独立 worker 时接口不变。

为什么不在请求里同步跑完：单条真实判别最长可达阶段一 120 秒 + 阶段二 300 秒，
[plan/02 §5](../../../plan/02-架构与详细设计.md) 明确“页面轮询任务状态，不维持一条
数分钟的模型请求连接”。因此改为后台线程执行 + 前端轮询。
"""

from __future__ import annotations

import json
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
    BudgetLedger,
    InMemoryBudgetLedger,
    result_to_payload,
    run_single,
)
from ..agent.stage1 import STAGE1_PROMPT_VERSION
from ..agent.stage2 import STAGE2_PROMPT_VERSION
from ..config import AppConfig
from ..llm.client import STAGE1, STAGE2, ModelClient, ModelResponse
from ..schemas.analysis import STAGE1_SCHEMA_VERSION
from ..schemas.judgement import STAGE2_SCHEMA_VERSION

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

    **边界**：这里的计数只在「账本从空开始」时等于阶段尝试序号（当前每次判别
    都新建内存账本，故成立）。S04 换成持久化账本后，尝试序号必须改从账本读，
    否则恢复场景会从 1 重新计数。
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


__all__ = [
    "COMPLETED",
    "FAILED",
    "PARTIAL_FAILED",
    "QUEUED",
    "RUNNING",
    "JudgeJob",
    "JudgeJobRegistry",
    "STAGE1",
    "STAGE2",
]
