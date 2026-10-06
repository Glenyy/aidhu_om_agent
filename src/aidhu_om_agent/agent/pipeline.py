"""S03-05：两阶段固定顺序与统一有限重试执行器。

固定顺序：阶段一（仅 q 与 ref）→ 校验 → 阶段二（q、a、全部原 ref、阶段一结果）
→ 校验。**阶段一充分性为 insufficient/uncertain 时仍执行阶段二**
（[plan/02 §3](../../../plan/02-架构与详细设计.md)）。

重试规则：

- 每阶段最多 ``execution.max_attempts_per_stage_campaign`` 次尝试（含首次），
  由本执行器统一管理；模型客户端已关闭 SDK 自带重试，不叠加。
- 可重试的模型错误按 ``execution.retry_backoff_seconds``（默认 2、4 秒）退避后重试。
- 校验失败把**简短的问题摘要**回传给模型后重试，不额外增加常规第三阶段。
- 不可重试的模型错误、上下文超限、输出截断直接失败。
- 任何失败都**不给标签**，不伪造判断。

预算通过 `BudgetLedger` 接口表示：本阶段为内存实现，S04 迁移为 SQLite 持久化。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..config import ExecutionConfig
from ..llm.client import STAGE1, STAGE2, ModelClient, ModelResponse, ModelUsage
from ..llm.errors import ModelCallError
from ..schemas.analysis import Stage1Analysis
from ..schemas.judgement import Stage2Judgement
from ..schemas.qa import QARecord
from .stage1 import build_stage1_messages
from .stage2 import build_stage2_messages
from .validation import (
    KIND_CONSISTENCY,
    KIND_EMPTY_QUOTE,
    KIND_EMPTY_REF,
    KIND_EVIDENCE_SUBSTRING,
    KIND_FIELD_SCHEMA,
    KIND_LABEL_ENUM,
    KIND_OUTPUT_FORMAT,
    KIND_REF_RANGE,
    ValidationError,
    parse_stage1,
    parse_stage2,
)

RecordStatus = Literal["completed", "failed"]
AttemptOutcome = Literal["ok", "validation_error", "model_error"]

#: 校验反复失败时的失败码；不是业务标签。
CODE_OUTPUT_INVALID = "OUTPUT_INVALID"
CODE_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

#: 被拒原始输出的留存上限；超出时保留头部与尾部，见 `_clip_raw_output`。
RAW_OUTPUT_LIMIT = 8000
_RAW_OUTPUT_HEAD = 6000
_RAW_OUTPUT_TAIL = 2000


@dataclass(frozen=True)
class StageAttempt:
    """一次阶段尝试的记录；``attempt`` 从 1 开始。

    ``raw_output`` 只在 ``outcome == "validation_error"`` 时非空，保存**被校验拒绝
    的那一次模型输出正文**，用于事后判读「模型到底写错了哪个字符」。它只含
    ``content``，不含任何推理内容；超过 ``RAW_OUTPUT_LIMIT`` 时截断并置
    ``raw_output_truncated``。
    """

    stage: str
    attempt: int
    outcome: AttemptOutcome
    model: str | None = None
    usage: ModelUsage | None = None
    latency_ms: int | None = None
    simulated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    raw_output: str | None = None
    raw_output_truncated: bool = False


@dataclass(frozen=True)
class RecordFailure:
    """记录级技术失败；``code`` 是工程错误码，不是业务标签。"""

    stage: str
    code: str
    message: str
    attempt_count: int
    retryable: bool


@dataclass(frozen=True)
class RecordResult:
    """单条记录的完整结果。

    失败时 ``stage1``/``stage2`` 可能为 ``None``；**不填造标签**。
    ``simulated`` 为 ``True`` 时结果来自模拟客户端，不得当作真实模型输出。
    """

    record_id: str | None
    source_row: int
    status: RecordStatus
    stage1: Stage1Analysis | None = None
    stage2: Stage2Judgement | None = None
    failure: RecordFailure | None = None
    simulated: bool = False
    attempts: tuple[StageAttempt, ...] = ()


class BudgetLedger(Protocol):
    """阶段预算账本；S03 用内存实现，S04 换 SQLite 持久化实现。"""

    def attempts_used(self, record_key: str, stage: str) -> int:
        """已占用且不再退还的尝试次数。"""

    def record_attempt(self, record_key: str, stage: str) -> int:
        """调用模型**之前**登记一次尝试占位，返回登记后的已用次数。"""

    def reset(self, record_key: str, stage: str) -> None:
        """显式重开一轮预算时清零；普通恢复不调用本方法。"""


class InMemoryBudgetLedger:
    """S03 的内存账本；进程重启即丢失，不承诺中断恢复。"""

    def __init__(self) -> None:
        self._used: dict[tuple[str, str], int] = {}

    def attempts_used(self, record_key: str, stage: str) -> int:
        return self._used.get((record_key, stage), 0)

    def record_attempt(self, record_key: str, stage: str) -> int:
        key = (record_key, stage)
        used = self._used.get(key, 0) + 1
        self._used[key] = used
        return used

    def reset(self, record_key: str, stage: str) -> None:
        self._used.pop((record_key, stage), None)


def record_key_of(record: QARecord) -> str:
    """预算账本的键；编号缺失时用来源行，保证唯一定位一条记录。"""
    return record.record_id if record.record_id else f"row:{record.source_row}"


#: 按校验问题的 ``kind`` 追加的**可操作**纠错提示。
#: 未命中的 kind（含 ``None``）走下面的通用提示，行为与本次改动前一致。
#: 键取 `agent.validation` 的 ``KIND_*`` 常量值，改名要同步。
_FEEDBACK_HINTS: dict[str, str] = {
    KIND_EVIDENCE_SUBSTRING: (
        "证据摘录必须是对应 ref 原文中**一段连续的文字**：回到原文原样复制，"
        "保留原有的空格、换行、标点、加粗符号与全角字符，不要“清理”或重排；"
        "不要抄写 [refN] 这类编号标记，不要加省略号或引号，不要拼接不相邻的内容。"
        "摘录尽量短，并优先选不含加粗符号、不含空行、不以空格开头的一句话或半句；"
        "如果对不上，就换一段更短、更干净的原文。"
    ),
    KIND_EMPTY_REF: "证据引用的 ref 在本次输入中是空资料，请改引其它非空 ref，或改用资料缺口说明。",
    KIND_EMPTY_QUOTE: "证据摘录不能为空，请从对应 ref 原文中复制一段连续文字。",
    KIND_REF_RANGE: "证据的 ref 编号只能用 ref1—ref10，请按输入中的实际编号填写。",
    KIND_LABEL_ENUM: "判断只能取三个既定标签之一，请照抄标签原文，不要改写或自造。",
    KIND_FIELD_SCHEMA: "请严格按要求的字段名与类型输出，不要增删字段或改变取值形式。",
    KIND_OUTPUT_FORMAT: "请只输出一个完整的 JSON 对象，不要输出其它文字。",
    KIND_CONSISTENCY: "字段之间不能自相矛盾，请按规则修正后重新输出。",
}


def _feedback_hint(error: ValidationError) -> str:
    """按错误分类取可操作提示；未知分类回落到通用提示。"""
    return _FEEDBACK_HINTS.get(
        error.kind or "",
        "请对照要求逐项检查输出内容，修正后重新输出。",
    )


def _feedback_messages(
    messages: Sequence[Mapping[str, str]], previous_output: str, error: ValidationError
) -> list[dict[str, str]]:
    """把上一次输出与校验问题追加到对话，作为下一次的纠错反馈。"""
    return [
        *[dict(message) for message in messages],
        {"role": "assistant", "content": previous_output or "（上一次输出为空）"},
        {
            "role": "user",
            "content": (
                f"上一次输出未通过校验：{error}。"
                f"{_feedback_hint(error)}"
                "请只重新输出一个符合要求的 JSON 对象，不要输出其它文字或代码块以外的内容。"
            ),
        },
    ]


def _clip_raw_output(text: str | None) -> tuple[str | None, bool]:
    """截断被拒原始输出，保留头部与尾部（尾部常含 JSON 的 evidence 字段）。"""
    if not text:
        return None, False
    if len(text) <= RAW_OUTPUT_LIMIT:
        return text, False
    return text[:_RAW_OUTPUT_HEAD] + "\n……（中间已省略）……\n" + text[-_RAW_OUTPUT_TAIL:], True


def _backoff_seconds(execution: ExecutionConfig, attempt: int) -> float:
    """第 ``attempt`` 次尝试失败后等待的秒数；用尽后沿用最后一个值。"""
    schedule = execution.retry_backoff_seconds
    if not schedule:
        return 0.0
    return float(schedule[min(attempt - 1, len(schedule) - 1)])


def _run_stage(
    *,
    stage: str,
    record_key: str,
    base_messages: Sequence[Mapping[str, str]],
    parse: Callable[[str], Any],
    client: ModelClient,
    execution: ExecutionConfig,
    ledger: BudgetLedger,
    attempts: list[StageAttempt],
    sleep: Callable[[float], None],
) -> tuple[Any | None, RecordFailure | None]:
    """执行一个阶段的“调用 → 校验 → 有限重试”，返回结果或失败。"""
    max_attempts = execution.max_attempts_per_stage_campaign
    messages: list[dict[str, str]] = [dict(message) for message in base_messages]

    while True:
        used = ledger.attempts_used(record_key, stage)
        if used >= max_attempts:
            return None, RecordFailure(
                stage=stage,
                code=CODE_BUDGET_EXHAUSTED,
                message=f"{stage} 阶段预算已用尽（上限 {max_attempts} 次）",
                attempt_count=used,
                retryable=False,
            )

        attempt_no = ledger.record_attempt(record_key, stage)
        try:
            response: ModelResponse = client.call(messages, stage)
        except ModelCallError as exc:
            attempts.append(
                StageAttempt(
                    stage=stage,
                    attempt=attempt_no,
                    outcome="model_error",
                    error_code=exc.code,
                    error_message=str(exc),
                )
            )
            if exc.retryable and attempt_no < max_attempts:
                sleep(_backoff_seconds(execution, attempt_no))
                continue
            return None, RecordFailure(
                stage=stage,
                code=exc.code,
                message=str(exc),
                attempt_count=attempt_no,
                retryable=exc.retryable,
            )

        try:
            parsed = parse(response.content)
        except ValidationError as exc:
            raw_output, raw_truncated = _clip_raw_output(response.content)
            attempts.append(
                StageAttempt(
                    stage=stage,
                    attempt=attempt_no,
                    outcome="validation_error",
                    model=response.model,
                    usage=response.usage,
                    latency_ms=response.latency_ms,
                    simulated=response.simulated,
                    error_code=CODE_OUTPUT_INVALID,
                    error_message=str(exc),
                    raw_output=raw_output,
                    raw_output_truncated=raw_truncated,
                )
            )
            if attempt_no >= max_attempts:
                return None, RecordFailure(
                    stage=stage,
                    code=CODE_OUTPUT_INVALID,
                    message=str(exc),
                    attempt_count=attempt_no,
                    retryable=False,
                )
            messages = _feedback_messages(messages, response.content, exc)
            continue

        attempts.append(
            StageAttempt(
                stage=stage,
                attempt=attempt_no,
                outcome="ok",
                model=response.model,
                usage=response.usage,
                latency_ms=response.latency_ms,
                simulated=response.simulated,
            )
        )
        return parsed, None


def run_single(
    record: QARecord,
    client: ModelClient,
    *,
    execution: ExecutionConfig,
    budget: BudgetLedger | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RecordResult:
    """按固定两阶段流程判别一条记录。

    ``client`` 是协议：真实实现为 `llm.client.OpenAICompatibleClient`，模拟实现为
    `agent.mock_samples.MockClient`。``sleep`` 可注入，便于测试不实际等待退避。
    """
    ledger = budget if budget is not None else InMemoryBudgetLedger()
    key = record_key_of(record)
    attempts: list[StageAttempt] = []

    stage1, failure = _run_stage(
        stage=STAGE1,
        record_key=key,
        base_messages=build_stage1_messages(record),
        parse=lambda text: parse_stage1(text, record),
        client=client,
        execution=execution,
        ledger=ledger,
        attempts=attempts,
        sleep=sleep,
    )
    if failure is not None:
        return _failed(record, failure, attempts)

    # 充分性为 insufficient/uncertain 时**仍然**进入阶段二。
    stage2, failure = _run_stage(
        stage=STAGE2,
        record_key=key,
        base_messages=build_stage2_messages(record, stage1),
        parse=lambda text: parse_stage2(text, record),
        client=client,
        execution=execution,
        ledger=ledger,
        attempts=attempts,
        sleep=sleep,
    )
    if failure is not None:
        return _failed(record, failure, attempts)

    return RecordResult(
        record_id=record.record_id,
        source_row=record.source_row,
        status="completed",
        stage1=stage1,
        stage2=stage2,
        failure=None,
        simulated=any(attempt.simulated for attempt in attempts),
        attempts=tuple(attempts),
    )


def _failed(
    record: QARecord, failure: RecordFailure, attempts: list[StageAttempt]
) -> RecordResult:
    return RecordResult(
        record_id=record.record_id,
        source_row=record.source_row,
        status="failed",
        stage1=None,
        stage2=None,
        failure=failure,
        simulated=any(attempt.simulated for attempt in attempts),
        attempts=tuple(attempts),
    )


def result_to_payload(result: RecordResult) -> dict[str, Any]:
    """把记录结果转成可直接返回前端的 JSON 结构。

    失败记录 ``label`` 为 ``None``；不返回模型推理内容。``attempts`` 中仅**校验
    失败**的那几次会带 ``raw_output``（模型输出正文，非推理链），便于事后判读失败
    原因；成功尝试不重复留存。
    """
    return {
        "record_id": result.record_id,
        "source_row": result.source_row,
        "status": result.status,
        "label": result.stage2.label.value if result.stage2 else None,
        "simulated": result.simulated,
        "stage1": result.stage1.model_dump(mode="json") if result.stage1 else None,
        "stage2": result.stage2.model_dump(mode="json") if result.stage2 else None,
        "failure": (
            {
                "stage": result.failure.stage,
                "code": result.failure.code,
                "message": result.failure.message,
                "attempt_count": result.failure.attempt_count,
                "retryable": result.failure.retryable,
            }
            if result.failure
            else None
        ),
        "attempts": [
            {
                "stage": attempt.stage,
                "attempt": attempt.attempt,
                "outcome": attempt.outcome,
                "model": attempt.model,
                "latency_ms": attempt.latency_ms,
                "simulated": attempt.simulated,
                "usage": (
                    {
                        "prompt_tokens": attempt.usage.prompt_tokens,
                        "completion_tokens": attempt.usage.completion_tokens,
                        "total_tokens": attempt.usage.total_tokens,
                    }
                    if attempt.usage
                    else None
                ),
                "error_code": attempt.error_code,
                "error_message": attempt.error_message,
                "raw_output": attempt.raw_output,
                "raw_output_truncated": attempt.raw_output_truncated,
            }
            for attempt in result.attempts
        ],
    }


__all__ = [
    "CODE_BUDGET_EXHAUSTED",
    "CODE_OUTPUT_INVALID",
    "RAW_OUTPUT_LIMIT",
    "BudgetLedger",
    "InMemoryBudgetLedger",
    "RecordFailure",
    "RecordResult",
    "RecordStatus",
    "StageAttempt",
    "record_key_of",
    "result_to_payload",
    "run_single",
]
