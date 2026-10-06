"""S03-05 的两阶段执行器单测：固定顺序、有限重试、预算与失败不填标签。

全部使用替身客户端，不发真实请求；``sleep`` 被注入以便断言退避而不真的等待。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from aidhu_om_agent.agent.pipeline import (
    CODE_BUDGET_EXHAUSTED,
    CODE_OUTPUT_INVALID,
    InMemoryBudgetLedger,
    record_key_of,
    result_to_payload,
    run_single,
)
from aidhu_om_agent.config import ExecutionConfig
from aidhu_om_agent.llm.client import STAGE1, STAGE2, ModelResponse
from aidhu_om_agent.llm.errors import (
    ModelConfigError,
    PermanentModelError,
    RetryableModelError,
    TruncatedOutputError,
)
from aidhu_om_agent.schemas.qa import REF_FIELDS, QARecord

REF1 = "校园卡补办需携带学生证，到校园卡中心办理。"


def make_record(**overrides: object) -> QARecord:
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs["ref1"] = REF1
    data: dict[str, object] = {
        "record_id": "1",
        "source_row": 2,
        "order_index": 0,
        "q": "如何补办校园卡？",
        "a": "带学生证去校园卡中心。",
        "refs": refs,
    }
    data.update(overrides)
    return QARecord(**data)  # type: ignore[arg-type]


STAGE1_OK: dict[str, object] = {
    "required_points": ["办理地点与所需材料"],
    "evidence_sufficiency": "sufficient",
    "evidence": [{"ref_id": "ref1", "quote": "需携带学生证"}],
    "missing_information": [],
    "conflicts": [],
    "reason": "ref1 覆盖核心作答要点。",
}

STAGE1_INSUFFICIENT: dict[str, object] = {
    "required_points": ["办理地点与所需材料"],
    "evidence_sufficiency": "insufficient",
    "evidence": [],
    "missing_information": ["资料未涉及所需材料"],
    "conflicts": [],
    "reason": "资料不足。",
}

STAGE2_OK: dict[str, object] = {
    "label": "回答正确",
    "reason": "回答与资料一致。",
    "issues": [],
    "evidence_sufficiency": "sufficient",
    "evidence": [{"ref_id": "ref1", "quote": "需携带学生证"}],
    "review_required": False,
    "review_reasons": [],
    "stage1_corrections": [],
}


def response(payload: Mapping[str, Any], *, simulated: bool = False) -> ModelResponse:
    return ModelResponse(
        content=json.dumps(dict(payload), ensure_ascii=False),
        model="stub-model",
        usage=None,
        latency_ms=5,
        finish_reason="stop",
        simulated=simulated,
    )


class ScriptedClient:
    """按阶段顺序吐出脚本项；项可以是 ModelResponse 或待抛出的异常。"""

    def __init__(self, script: Mapping[str, Sequence[Any]]) -> None:
        self._script = {stage: list(items) for stage, items in script.items()}
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def call(self, messages: Sequence[Mapping[str, str]], stage: str) -> ModelResponse:
        self.calls.append((stage, [dict(message) for message in messages]))
        queue = self._script.get(stage, [])
        if not queue:
            raise AssertionError(f"{stage} 的脚本已用尽，执行器多调了一次")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def stages(self) -> list[str]:
        return [stage for stage, _ in self.calls]


def execution(max_attempts: int = 3) -> ExecutionConfig:
    return ExecutionConfig(
        concurrency=1,
        max_attempts_per_stage_campaign=max_attempts,
        retry_backoff_seconds=(2, 4),
    )


def run(script: Mapping[str, Sequence[Any]], **kwargs: Any) -> Any:
    client = ScriptedClient(script)
    waits: list[float] = []
    result = run_single(
        kwargs.pop("record", make_record()),
        client,
        execution=kwargs.pop("execution", execution()),
        budget=kwargs.pop("budget", None),
        sleep=waits.append,
    )
    return result, client, waits


# ---------------------------------------------------------------- 成功路径


def test_happy_path_runs_both_stages_in_order() -> None:
    result, client, waits = run({STAGE1: [response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]})

    assert result.status == "completed"
    assert result.stage1 is not None and result.stage2 is not None
    assert result.stage2.label.value == "回答正确"
    assert client.stages == [STAGE1, STAGE2]
    assert waits == []
    assert [(attempt.stage, attempt.attempt, attempt.outcome) for attempt in result.attempts] == [
        (STAGE1, 1, "ok"),
        (STAGE2, 1, "ok"),
    ]


def test_insufficient_stage1_still_runs_stage2() -> None:
    # 资料不足不短路：阶段二仍要判别「未检索到正确资料」。
    stage2 = {**STAGE2_OK, "label": "未检索到正确资料", "evidence_sufficiency": "insufficient", "evidence": []}
    result, client, _ = run(
        {STAGE1: [response(STAGE1_INSUFFICIENT)], STAGE2: [response(stage2)]}
    )

    assert client.stages == [STAGE1, STAGE2]
    assert result.status == "completed"
    assert result.stage2 is not None
    assert result.stage2.label.value == "未检索到正确资料"


def test_first_stage_request_has_no_answer() -> None:
    record = make_record()
    _, client, _ = run({STAGE1: [response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]}, record=record)
    stage1_messages = client.calls[0][1]

    assert all("带学生证去校园卡中心。" not in message["content"] for message in stage1_messages)


def test_second_stage_request_has_answer_and_stage1_result() -> None:
    record = make_record()
    _, client, _ = run({STAGE1: [response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]}, record=record)
    stage2_messages = client.calls[1][1]
    joined = "\n".join(message["content"] for message in stage2_messages)

    assert record.a is not None and record.a in joined
    assert REF1 in joined
    assert "ref1 覆盖核心作答要点。" in joined


def test_simulated_flag_propagates() -> None:
    result, _, _ = run(
        {STAGE1: [response(STAGE1_OK, simulated=True)], STAGE2: [response(STAGE2_OK, simulated=True)]}
    )

    assert result.simulated is True


# ---------------------------------------------------------------- 模型错误重试


def test_retryable_error_is_retried_with_backoff() -> None:
    result, client, waits = run(
        {STAGE1: [RetryableModelError("连接重置", stage=STAGE1), response(STAGE1_OK)],
         STAGE2: [response(STAGE2_OK)]}
    )

    assert result.status == "completed"
    assert waits == [2.0]
    assert [attempt.outcome for attempt in result.attempts] == ["model_error", "ok", "ok"]
    assert result.attempts[0].error_code == "MODEL_RETRYABLE"
    assert result.attempts[1].attempt == 2


def test_retryable_error_gives_up_at_the_campaign_limit() -> None:
    result, client, waits = run(
        {STAGE1: [RetryableModelError("慢", stage=STAGE1), RetryableModelError("仍慢", stage=STAGE1)]},
        execution=execution(max_attempts=2),
    )

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == "MODEL_RETRYABLE"
    assert result.failure.attempt_count == 2
    assert result.failure.retryable is True
    assert client.stages == [STAGE1, STAGE1]
    assert waits == [2.0]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PermanentModelError("认证失败", stage=STAGE1), "MODEL_PERMANENT"),
        (TruncatedOutputError("输出截断", stage=STAGE1), "OUTPUT_TRUNCATED"),
        (ModelConfigError("缺少凭据", stage=STAGE1), "CONFIG_INVALID"),
    ],
)
def test_non_retryable_errors_fail_immediately(error: Exception, code: str) -> None:
    result, client, waits = run({STAGE1: [error], STAGE2: [response(STAGE2_OK)]})

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == code
    assert result.failure.retryable is False
    assert client.stages == [STAGE1]
    assert waits == []


def test_stage2_failure_keeps_no_label() -> None:
    result, client, _ = run(
        {STAGE1: [response(STAGE1_OK)], STAGE2: [PermanentModelError("模型不存在", stage=STAGE2)]}
    )

    assert result.stage2 is None
    assert result_to_payload(result)["label"] is None
    assert client.stages == [STAGE1, STAGE2]


# ---------------------------------------------------------------- 校验失败重试


def test_validation_error_feeds_the_problem_back_and_retries() -> None:
    broken = ModelResponse(
        content="我认为这条回答正确。",
        model="stub-model",
        usage=None,
        latency_ms=3,
        finish_reason="stop",
    )
    result, client, waits = run(
        {STAGE1: [broken, response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]}
    )

    assert result.status == "completed"
    assert waits == []  # 校验失败不走退避
    assert result.attempts[0].outcome == "validation_error"
    assert result.attempts[0].error_code == CODE_OUTPUT_INVALID

    retry_messages = client.calls[1][1]
    assert retry_messages[-2]["role"] == "assistant"
    assert retry_messages[-2]["content"] == "我认为这条回答正确。"
    assert retry_messages[-1]["role"] == "user"
    assert "上一次输出未通过校验" in retry_messages[-1]["content"]
    # 反馈里带上了原始请求，模型仍能看到 q 与 ref。
    assert any("如何补办校园卡？" in message["content"] for message in retry_messages)


def test_repeated_invalid_output_fails_with_output_invalid() -> None:
    broken = ModelResponse(
        content='{"reason": "缺字段"}',
        model="stub-model",
        usage=None,
        latency_ms=1,
        finish_reason="stop",
    )
    result, client, _ = run({STAGE1: [broken, broken, broken]})

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == CODE_OUTPUT_INVALID
    assert result.failure.attempt_count == 3
    assert result.failure.retryable is False
    assert client.stages == [STAGE1, STAGE1, STAGE1]


def test_empty_output_is_a_validation_problem_not_a_crash() -> None:
    empty = ModelResponse(
        content="", model="stub-model", usage=None, latency_ms=1, finish_reason="stop"
    )
    result, _, _ = run({STAGE1: [empty, response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]})

    assert result.status == "completed"
    assert result.attempts[0].error_message is not None
    assert "为空" in result.attempts[0].error_message


# ---------------------------------------------------------------- 预算


def test_budget_exhausted_fails_without_calling_the_model() -> None:
    budget = InMemoryBudgetLedger()
    budget.record_attempt("1", STAGE1)
    result, client, _ = run(
        {STAGE1: [response(STAGE1_OK)]}, execution=execution(max_attempts=1), budget=budget
    )

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == CODE_BUDGET_EXHAUSTED
    assert result.failure.attempt_count == 1
    assert client.calls == []


def test_budget_counts_each_attempt_including_failures() -> None:
    budget = InMemoryBudgetLedger()
    run(
        {STAGE1: [RetryableModelError("x", stage=STAGE1), response(STAGE1_OK)],
         STAGE2: [response(STAGE2_OK)]},
        budget=budget,
    )

    assert budget.attempts_used("1", STAGE1) == 2
    assert budget.attempts_used("1", STAGE2) == 1


def test_budget_is_keyed_by_record() -> None:
    budget = InMemoryBudgetLedger()
    budget.record_attempt("row:9", STAGE1)

    assert budget.attempts_used("row:9", STAGE1) == 1
    assert budget.attempts_used("1", STAGE1) == 0
    budget.reset("row:9", STAGE1)
    assert budget.attempts_used("row:9", STAGE1) == 0


# ---------------------------------------------------------------- 结果转换


def test_record_key_prefers_id_then_falls_back_to_row() -> None:
    assert record_key_of(make_record()) == "1"
    assert record_key_of(make_record(record_id=None)) == "row:2"


def test_payload_omits_label_and_has_failure_on_failure() -> None:
    result, _, _ = run({STAGE1: [PermanentModelError("失败", stage=STAGE1)]})
    payload = result_to_payload(result)

    assert payload["status"] == "failed"
    assert payload["label"] is None
    assert payload["stage1"] is None and payload["stage2"] is None
    assert payload["failure"]["code"] == "MODEL_PERMANENT"
    assert payload["simulated"] is False


def test_payload_contains_stage_details_and_attempts() -> None:
    result, _, _ = run({STAGE1: [response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]})
    payload = result_to_payload(result)

    assert payload["label"] == "回答正确"
    assert payload["stage1"]["evidence"][0]["ref_id"] == "ref1"
    assert payload["stage2"]["label"] == "回答正确"
    assert [item["stage"] for item in payload["attempts"]] == [STAGE1, STAGE2]
    # 返回前端的结构固定；业务「判断」只有 label，充分性属于阶段详情。
    assert set(payload) == {
        "record_id",
        "source_row",
        "status",
        "label",
        "simulated",
        "stage1",
        "stage2",
        "failure",
        "attempts",
    }


# ----------------------------------------------- 2026-10-06 返工：被拒输出留存


def broken_response(content: str) -> ModelResponse:
    return ModelResponse(
        content=content,
        model="stub-model",
        usage=None,
        latency_ms=7,
        finish_reason="stop",
    )


#: 结构合法、摘录却不是 ref1 原文子串：复现用户真实文件上遇到的失败类型。
BAD_QUOTE_STAGE2 = {
    **STAGE2_OK,
    "evidence": [{"ref_id": "ref1", "quote": "这段摘录在原文里并不存在"}],
}


def test_rejected_output_is_retained_on_the_attempt() -> None:
    bad = response(BAD_QUOTE_STAGE2)
    result, _, _ = run(
        {STAGE1: [response(STAGE1_OK)], STAGE2: [bad, bad, bad]}, execution=execution(1)
    )

    attempt = result.attempts[-1]
    assert attempt.outcome == "validation_error"
    assert attempt.raw_output == bad.content
    assert attempt.raw_output_truncated is False


def test_long_rejected_output_is_truncated_keeping_head_and_tail() -> None:
    padding = "冗" * 20000
    long_payload = {
        **STAGE2_OK,
        "reason": padding,
        "evidence": [{"ref_id": "ref1", "quote": "尾部的伪造摘录"}],
    }
    bad = response(long_payload)
    result, _, _ = run({STAGE1: [response(STAGE1_OK)], STAGE2: [bad]}, execution=execution(1))

    attempt = result.attempts[-1]
    assert attempt.raw_output_truncated is True
    assert attempt.raw_output is not None
    assert len(attempt.raw_output) < len(bad.content)
    assert attempt.raw_output.startswith(bad.content[:100])
    # 尾部必须保留：JSON 的 evidence 字段就在这里。
    assert attempt.raw_output.endswith(bad.content[-100:])


def test_successful_attempts_do_not_retain_output() -> None:
    result, _, _ = run({STAGE1: [response(STAGE1_OK)], STAGE2: [response(STAGE2_OK)]})

    assert all(attempt.raw_output is None for attempt in result.attempts)


def test_evidence_substring_failure_gets_an_actionable_hint() -> None:
    bad = response(BAD_QUOTE_STAGE2)
    result, client, _ = run(
        {
            STAGE1: [response(STAGE1_OK)],
            STAGE2: [bad, response(STAGE2_OK)],
        }
    )

    feedback = client.calls[-1][1][-1]["content"]
    assert result.status == "completed"
    assert "上一次输出未通过校验" in feedback
    assert "ref1 原文的真实子串" in feedback
    assert "连续" in feedback and "不要抄" in feedback


def test_unknown_validation_kind_falls_back_to_generic_hint() -> None:
    from aidhu_om_agent.agent.pipeline import _feedback_hint
    from aidhu_om_agent.agent.validation import ValidationError

    assert "逐项检查" in _feedback_hint(ValidationError("别的问题"))
    assert "逐项检查" in _feedback_hint(ValidationError("别的问题", kind="未登记的分类"))


def test_payload_attempts_carry_raw_output_keys() -> None:
    bad = response(BAD_QUOTE_STAGE2)
    result, _, _ = run({STAGE1: [response(STAGE1_OK)], STAGE2: [bad]}, execution=execution(1))
    payload = result_to_payload(result)

    attempt = payload["attempts"][-1]
    assert attempt["raw_output"] == bad.content
    assert attempt["raw_output_truncated"] is False
    # 成功尝试同样带这两个键，只是值为 None / False。
    assert set(payload["attempts"][0]) >= {"raw_output", "raw_output_truncated"}
