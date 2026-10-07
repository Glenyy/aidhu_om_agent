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

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..config import ExecutionConfig
from ..llm.client import STAGE1, STAGE2, ModelClient, ModelResponse, ModelUsage
from ..llm.errors import ModelCallError
from ..schemas.analysis import STAGE1_SCHEMA_VERSION
from ..schemas.analysis import STAGE1_SCHEMA_VERSION, Stage1Analysis
from ..schemas.judgement import STAGE2_SCHEMA_VERSION, Stage2Judgement
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

    def complete_attempt(
        self, record_key: str, stage: str, attempt_no: int, completion: "AttemptCompletion"
    ) -> None:
        """一次尝试结束后回报结果；成功时会连同阶段结果一并提交。

        **持久化实现在这里开短事务**：写入 attempt 终态、`stage_results`、记录
        状态/投影与 `runs.revision`（[plan/09 §…“保存阶段”]）。内存实现为空操作。
        实现抛出的存储异常直接向上传播，由调用方决定任务级别处理。
        """

    def reset(self, record_key: str, stage: str) -> None:
        """显式重开一轮预算时清零；普通恢复不调用本方法。"""


@dataclass(frozen=True)
class AttemptCompletion:
    """一次尝试结束后的回报（S04-03）。

    它同时承载两件事：**尝试的终态**（供 `call_attempts` 落库）与**成功时的阶段
    结果**（供 `stage_results` 落库）。``result``/``result_json``/``schema_version``
    只在 ``outcome == "ok"`` 时非空。

    ``content`` 是模型返回的**最终正文**，已按 `RAW_OUTPUT_LIMIT` 截断；持久化实现
    按 plan/09 §4 落 `call_attempts.final_content`，接口层仍只对失败尝试暴露它
    （S03 返工 R-1 的口径不变）。
    """

    outcome: AttemptOutcome
    model: str | None = None
    usage: ModelUsage | None = None
    latency_ms: int | None = None
    simulated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    content: str | None = None
    content_truncated: bool = False
    result: Any | None = None
    result_json: str | None = None
    schema_version: str | None = None
    #: 阶段二成功时的记录投影；阶段一为 ``None``（那时还不允许有标签）。
    final_label: str | None = None
    review_required: bool | None = None


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

    def complete_attempt(
        self, record_key: str, stage: str, attempt_no: int, completion: AttemptCompletion
    ) -> None:
        """内存账本不保存尝试明细；阶段结果只随 `RecordResult` 返回。"""

    def reset(self, record_key: str, stage: str) -> None:
        self._used.pop((record_key, stage), None)


def record_key_of(record: QARecord) -> str:
    """预算账本的键；编号缺失时用来源行，保证唯一定位一条记录。

    **只用于 S03 的临时判一条入口**（记录还没落库，没有内部主键）。执行持久化批次
    时必须改为传 `records.record_key`，见 `run_single(record_key=...)`。
    """
    return record.record_id if record.record_id else f"row:{record.source_row}"


def canonical_json(model: Any) -> str:
    """阶段结果的规范化 JSON 文本；`stage_results.result_json` 与摘要都取它。"""
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


#: 按校验问题的 ``kind`` 追加的**可操作**纠错提示。
#: 未命中的 kind（含 ``None``）走下面的通用提示，行为与本次改动前一致。
#: 键取 `agent.validation` 的 ``KIND_*`` 常量值，改名要同步。
_FEEDBACK_HINTS: dict[str, str] = {
    KIND_EVIDENCE_SUBSTRING: (
        "证据摘录必须是对应 ref 原文中**一段连续的文字**：回到原文原样复制，"
        "保留原有的空格、换行、标点、加粗符号与全角字符，不要“清理”或重排；"
        "不要抄写 [refN] 这类编号标记，不要加省略号或引号，不要拼接不相邻的内容。"
        "「逐字」说的是 JSON 解析之后的文本：换行在 JSON 里要写成 \\n，"
        "不要为了避开换行把跨行的原文接成一行、也不要删掉中间的换行；"
        "原文的列表符号（-、*、1. 等）照抄，不要换符号或补删符号。"
        "摘录尽量短，并优先选不含加粗符号、不含空行、不以空格开头的一句话或半句；"
        "如果对不上，就换一段更短、更干净的原文。"
    ),
    KIND_EMPTY_REF: "证据引用的 ref 在本次输入中是空资料，请改引其它非空 ref，或改用资料缺口说明。",
    KIND_EMPTY_QUOTE: "证据摘录不能为空，请从对应 ref 原文中复制一段连续文字。",
    KIND_REF_RANGE: "证据的 ref 编号只能用 ref1—ref10，请按输入中的实际编号填写。",
    KIND_LABEL_ENUM: "判断只能取三个既定标签之一，请照抄标签原文，不要改写或自造。",
    KIND_FIELD_SCHEMA: "请严格按要求的字段名与类型输出，不要增删字段或改变取值形式。",
    # 输出的格式问题里最容易反复出现的是**换行没转义**：字符串里出现真正的换行
    # 会让整个 JSON 无法解析（S03 遗留的「记录 4 失败族」机制之一），所以这一条
    # 不只说「输出 JSON」，还点名这一条怎么改。
    KIND_OUTPUT_FORMAT: (
        "请只输出一个完整的 JSON 对象，不要输出其它文字、解释或多余代码块。"
        "字符串里不能出现真正的换行：换行要写成 \\n（反斜杠 + n）；"
        "字符串里的引号写成 \\\"。"
    ),
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
    """执行一个阶段的“调用 → 校验 → 有限重试”，返回结果或失败。

    每次尝试都在**调用前**占位、在**返回后**回报终态：占位先于调用写入，所以
    「调用已发出但结果没记下」时预算仍然被占用（[plan/10 §5]）。
    """
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
            ledger.complete_attempt(
                record_key,
                stage,
                attempt_no,
                AttemptCompletion(
                    outcome="model_error",
                    error_code=exc.code,
                    error_message=str(exc),
                ),
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
            ledger.complete_attempt(
                record_key,
                stage,
                attempt_no,
                AttemptCompletion(
                    outcome="validation_error",
                    model=response.model,
                    usage=response.usage,
                    latency_ms=response.latency_ms,
                    simulated=response.simulated,
                    error_code=CODE_OUTPUT_INVALID,
                    error_message=str(exc),
                    content=raw_output,
                    content_truncated=raw_truncated,
                ),
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
        # 成功：持久化实现在**同一事务**里写 attempt 终态、stage_results、记录状态
        # 与批次 revision；解析与证据校验已经在上面的 parse() 里完成，不在事务内。
        content, content_truncated = _clip_raw_output(response.content)
        ledger.complete_attempt(
            record_key,
            stage,
            attempt_no,
            AttemptCompletion(
                outcome="ok",
                model=response.model,
                usage=response.usage,
                latency_ms=response.latency_ms,
                simulated=response.simulated,
                content=content,
                content_truncated=content_truncated,
                result=parsed,
                result_json=canonical_json(parsed),
                schema_version=(
                    STAGE1_SCHEMA_VERSION if stage == STAGE1 else STAGE2_SCHEMA_VERSION
                ),
                # 阶段一结果没有 label/review_required 字段，因此投影为 None；
                # 三分类标签只在阶段二结果里出现。
                final_label=getattr(getattr(parsed, "label", None), "value", None),
                review_required=getattr(parsed, "review_required", None),
            ),
        )
        return parsed, None


def run_single(
    record: QARecord,
    client: ModelClient,
    *,
    execution: ExecutionConfig,
    budget: BudgetLedger | None = None,
    record_key: str | None = None,
    prompts: Mapping[str, str] | None = None,
    stage1: Stage1Analysis | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RecordResult:
    """按固定两阶段流程判别一条记录。

    ``client`` 是协议：真实实现为 `llm.client.OpenAICompatibleClient`，模拟实现为
    `agent.mock_samples.MockClient`。``sleep`` 可注入，便于测试不实际等待退避。

    ``record_key`` 是**持久化批次的内部主键**（`records.record_key`）；执行已落库的
    记录时必须传它，否则会退回 S03 的临时键（编号或来源行）。``prompts`` 是批次
    冻结的提示词快照（``{"stage1": 正文, "stage2": 正文}``），缺省时读磁盘当前版本。
    ``stage1`` 非空表示阶段一**已提交**，直接从阶段二开始，不重跑阶段一也不占用
    它的预算（恢复路径，[plan/10 §3]）。
    """
    ledger = budget if budget is not None else InMemoryBudgetLedger()
    key = record_key if record_key is not None else record_key_of(record)
    attempts: list[StageAttempt] = []

    if stage1 is None:
        stage1, failure = _run_stage(
            stage=STAGE1,
            record_key=key,
            base_messages=build_stage1_messages(
                record, prompt=None if prompts is None else prompts[STAGE1]
            ),
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
        base_messages=build_stage2_messages(
            record,
            stage1,
            prompt=None if prompts is None else prompts[STAGE2],
        ),
        parse=lambda text: parse_stage2(text, record),
        client=client,
        execution=execution,
        ledger=ledger,
        attempts=attempts,
        sleep=sleep,
    )
    if failure is not None:
        # 阶段二失败**保留阶段一结果**（[plan/10 §3]）：调用方据此保持已提交的
        # 阶段一检查点，不给标签。
        return _failed(record, failure, attempts, stage1=stage1)

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
    record: QARecord,
    failure: RecordFailure,
    attempts: list[StageAttempt],
    *,
    stage1: Stage1Analysis | None = None,
) -> RecordResult:
    """失败结果；**不填造标签**。阶段一已提交时保留其原结果。"""
    return RecordResult(
        record_id=record.record_id,
        source_row=record.source_row,
        status="failed",
        stage1=stage1,
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
    "AttemptCompletion",
    "BudgetLedger",
    "InMemoryBudgetLedger",
    "RecordFailure",
    "RecordResult",
    "RecordStatus",
    "StageAttempt",
    "canonical_json",
    "record_key_of",
    "result_to_payload",
    "run_single",
]
