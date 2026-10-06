"""S03-02／S03-03 的请求边界单测。

核对两条硬边界：阶段一只含 q 与 ref1—ref10（不含 a）；阶段二含 q、a、**全部原始
ref** 与完整阶段一结果。这里只检查纯函数构造的消息，不调用模型。
"""

from __future__ import annotations

import pytest

from aidhu_om_agent.agent.stage1 import (
    STAGE1_PROMPT_VERSION,
    build_stage1_messages,
    stage1_messages_contain_a,
)
from aidhu_om_agent.agent.stage2 import (
    STAGE2_PROMPT_VERSION,
    build_stage2_messages,
    stage2_missing_refs,
)
from aidhu_om_agent.schemas.analysis import Stage1Analysis
from aidhu_om_agent.schemas.qa import REF_FIELDS, Evidence, QARecord

Q_TEXT = "如何补办校园卡？"
A_TEXT = "带学生证到校园卡中心办理即可。"
REF1 = "校园卡补办需携带学生证，到校园卡中心办理。"
REF2 = "办理时间为工作日 9:00—17:00。"
REF10 = "补办工本费 20 元。"


def make_record(**overrides: object) -> QARecord:
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs["ref1"] = REF1
    refs["ref2"] = REF2
    refs["ref10"] = REF10
    data: dict[str, object] = {
        "record_id": "1",
        "source_row": 2,
        "order_index": 0,
        "q": Q_TEXT,
        "a": A_TEXT,
        "refs": refs,
    }
    data.update(overrides)
    return QARecord(**data)  # type: ignore[arg-type]


def stage1_result(**overrides: object) -> Stage1Analysis:
    data: dict[str, object] = {
        "required_points": ("办理地点", "所需材料"),
        "evidence_sufficiency": "sufficient",
        "evidence": (Evidence(ref_id="ref1", quote="需携带学生证"),),
        "missing_information": (),
        "conflicts": (),
        "reason": "ref1 覆盖核心作答要点。",
    }
    data.update(overrides)
    return Stage1Analysis(**data)  # type: ignore[arg-type]


def _joined(messages: list[dict[str, str]]) -> str:
    return "\n".join(message["content"] for message in messages)


# ---------------------------------------------------------------- 阶段一


def test_stage1_has_two_roles_and_uses_prompt() -> None:
    messages = build_stage1_messages(make_record())

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"]
    assert STAGE1_PROMPT_VERSION == "1.1"


def test_stage1_carries_question_and_all_refs() -> None:
    text = _joined(build_stage1_messages(make_record()))

    assert Q_TEXT in text
    for ref in (REF1, REF2, REF10):
        assert ref in text


def test_stage1_never_carries_the_answer() -> None:
    messages = build_stage1_messages(make_record())

    assert A_TEXT not in _joined(messages)
    assert stage1_messages_contain_a(messages, make_record()) is False


def test_stage1_marks_empty_refs_without_dropping_them() -> None:
    text = _joined(build_stage1_messages(make_record()))

    for field in REF_FIELDS:
        assert f"[{field}]" in text
    assert text.count("（本份资料为空）") == len(REF_FIELDS) - 3


def test_stage1_rejects_record_without_question() -> None:
    with pytest.raises(ValueError, match="缺少 q"):
        build_stage1_messages(make_record(q=None))


def test_stage1_multi_line_ref_is_kept_verbatim() -> None:
    ref = "第一行\n第二行"
    text = _joined(build_stage1_messages(make_record(refs=_refs_with("ref3", ref))))

    assert ref in text


# ---------------------------------------------------------------- 阶段二


def test_stage2_carries_question_answer_and_all_refs() -> None:
    text = _joined(build_stage2_messages(make_record(), stage1_result()))

    assert Q_TEXT in text
    assert A_TEXT in text
    for ref in (REF1, REF2, REF10):
        assert ref in text
    assert stage2_missing_refs(build_stage2_messages(make_record(), stage1_result()), make_record()) == ()


def test_stage2_carries_full_stage1_result_not_a_summary() -> None:
    analysis = stage1_result(
        required_points=("办理地点",),
        missing_information=("缺少办理时间",),
        conflicts=("两份资料时间不一致",),
    )
    text = _joined(build_stage2_messages(make_record(), analysis))

    assert "办理地点" in text
    assert "缺少办理时间" in text
    assert "两份资料时间不一致" in text
    assert "需携带学生证" in text  # 阶段一证据原文一并交给阶段二复核
    assert "sufficient" in text  # 内部充分性取值原样传递


def test_stage2_reports_missing_refs() -> None:
    # 只发给模型的部分消息（模拟被截断）应能被 stage2_missing_refs 识别。
    truncated = [{"role": "user", "content": Q_TEXT}]

    assert set(stage2_missing_refs(truncated, make_record())) == {"ref1", "ref2", "ref10"}


def test_stage2_ignores_empty_refs_in_missing_check() -> None:
    messages = build_stage2_messages(make_record(), stage1_result())

    assert "ref3" not in stage2_missing_refs(messages, make_record())


@pytest.mark.parametrize("overrides", [{"q": None}, {"a": None}])
def test_stage2_rejects_incomplete_record(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build_stage2_messages(make_record(**overrides), stage1_result())


def test_stage2_uses_its_own_prompt() -> None:
    messages = build_stage2_messages(make_record(), stage1_result())

    assert messages[0]["role"] == "system"
    assert STAGE2_PROMPT_VERSION == "1.1"
    assert messages[0]["content"] != build_stage1_messages(make_record())[0]["content"]


# ---------------------------------------------------------------- 辅助


def _refs_with(ref_id: str, text: str) -> dict[str, str | None]:
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs[ref_id] = text
    return refs


# ------------------------------ 2026-10-06 返工：摘录规则与可操作提示仍在提示词里


def _system_prompt(messages: list[dict[str, str]]) -> str:
    return messages[0]["content"]


@pytest.mark.parametrize("builder", ["stage1", "stage2"])
def test_prompt_keeps_the_verbatim_substring_contract(builder: str) -> None:
    """契约句不能被"改进提示词"顺手改写：判定规则仍是原文逐字子串。"""
    messages = (
        build_stage1_messages(make_record())
        if builder == "stage1"
        else build_stage2_messages(make_record(), stage1_result())
    )
    prompt = _system_prompt(messages)

    assert "原文子串" in prompt
    assert "逐字复制" in prompt


@pytest.mark.parametrize("builder", ["stage1", "stage2"])
def test_prompt_tells_the_model_how_to_pick_a_quote(builder: str) -> None:
    """返工新增的可操作策略：针对真实文件里的 `**`、空行与行首空格。"""
    messages = (
        build_stage1_messages(make_record())
        if builder == "stage1"
        else build_stage2_messages(make_record(), stage1_result())
    )
    prompt = _system_prompt(messages)

    assert "照着做" in prompt
    assert "一段连续" in prompt
    assert "[refN]" in prompt
    assert "**" in prompt
    assert "空行" in prompt
    assert "省略号" in prompt
