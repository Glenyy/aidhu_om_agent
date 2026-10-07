"""S03-02／S03-03 的请求边界单测。

核对两条硬边界：阶段一只含 q 与 ref1—ref10（不含 a）；阶段二含 q、a、**全部原始
ref** 与完整阶段一结果。这里只检查纯函数构造的消息，不调用模型。
"""

from __future__ import annotations

import re

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
    # 只核形态：具体版本号由 `test_prompt_versions_are_bumped_but_schemas_are_unchanged`
    # 一处钉住，避免每次改提示词都要在多个地方同步一个数字。
    assert re.fullmatch(r"\d+\.\d+", STAGE1_PROMPT_VERSION)


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
    assert re.fullmatch(r"\d+\.\d+", STAGE2_PROMPT_VERSION)
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

# ---------------------------- S04-08：区分「逐字」与 JSON 字符串内的换行转义


def _prompt_of(builder: str) -> str:
    messages = (
        build_stage1_messages(make_record())
        if builder == "stage1"
        else build_stage2_messages(make_record(), stage1_result())
    )
    return _system_prompt(messages)


@pytest.mark.parametrize("builder", ["stage1", "stage2"])
def test_prompt_separates_verbatim_from_json_newline_escaping(builder: str) -> None:
    """「逐字」说的是 JSON 解析之后的文本；字符串里换行必须写成 ``\\n``。

    S03 遗留的失败族里，一种机制就是这两件事被混为一谈：模型一边被告知
    「原样保留换行」，一边必须在 JSON 字符串里写 ``\\n``——不点破它就会二选一
    错一个（写进真换行则 JSON 解析失败，删掉换行则摘录对不上原文）。
    """
    prompt = _prompt_of(builder)

    assert "\\n" in prompt  # 明确给出了转义写法
    assert "反斜杠" in prompt
    assert "解析之后" in prompt  # 逐字的判定时点
    assert "真正的换行" in prompt


@pytest.mark.parametrize("builder", ["stage1", "stage2"])
def test_prompt_forbids_rewriting_list_markers(builder: str) -> None:
    """列表符号属于原文：换符号、补删符号同样是改写，不是「排版整理」。"""
    prompt = _prompt_of(builder)

    assert "列表符号" in prompt
    assert "照抄" in prompt


@pytest.mark.parametrize("builder", ["stage1", "stage2"])
def test_prompt_forbids_joining_lines_to_dodge_the_escape(builder: str) -> None:
    """反向的偷懒也要堵住：为了避开换行把跨行原文接成一行，同样对不上原文。"""
    prompt = _prompt_of(builder)

    assert "接成一行" in prompt


def test_prompt_versions_are_bumped_but_schemas_are_unchanged() -> None:
    """S04-08 只改提示词：提示词版本递增，**输出契约（schema）不变**。"""
    from aidhu_om_agent.schemas.analysis import STAGE1_SCHEMA_VERSION
    from aidhu_om_agent.schemas.judgement import STAGE2_SCHEMA_VERSION

    assert STAGE1_PROMPT_VERSION == "1.2" and STAGE2_PROMPT_VERSION == "1.2"
    assert STAGE1_SCHEMA_VERSION == "1.1" and STAGE2_SCHEMA_VERSION == "1.1"
