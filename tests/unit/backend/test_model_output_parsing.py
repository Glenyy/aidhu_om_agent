"""S03-04 的模型输出解析单测。

覆盖两点：**只接受唯一 JSON 对象**（不猜答案），以及阶段一/阶段二字段间的
明显矛盾检查。语义充分性不由程序判定，因此这里也不假装断言它。
"""

from __future__ import annotations

import json

import pytest

from aidhu_om_agent.agent.validation import (
    ValidationError,
    extract_json_object,
    parse_stage1,
    parse_stage2,
    validate_stage1_consistency,
    validate_stage2_consistency,
)
from aidhu_om_agent.schemas.analysis import Stage1Analysis
from aidhu_om_agent.schemas.judgement import Label, Stage2Judgement
from aidhu_om_agent.schemas.qa import REF_FIELDS, Evidence, QARecord

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


def stage1_payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "required_points": ["办理地点与所需材料"],
        "evidence_sufficiency": "sufficient",
        "evidence": [{"ref_id": "ref1", "quote": "需携带学生证"}],
        "missing_information": [],
        "conflicts": [],
        "reason": "ref1 覆盖核心作答要点。",
    }
    data.update(overrides)
    return data


def stage2_payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "label": "回答正确",
        "reason": "回答与资料一致。",
        "issues": [],
        "evidence_sufficiency": "sufficient",
        "evidence": [{"ref_id": "ref1", "quote": "需携带学生证"}],
        "review_required": False,
        "review_reasons": [],
        "stage1_corrections": [],
    }
    data.update(overrides)
    return data


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def fenced(payload: dict[str, object], *, language: str = "json") -> str:
    return f"```{language}\n{dumps(payload)}\n```"


# ---------------------------------------------------------------- JSON 提取


def test_bare_json_object_is_accepted() -> None:
    assert extract_json_object(dumps(stage1_payload()))["reason"] == "ref1 覆盖核心作答要点。"


def test_single_fenced_block_is_accepted() -> None:
    assert extract_json_object(fenced(stage1_payload()))["required_points"]


def test_fence_without_language_is_accepted() -> None:
    text = f"```\n{dumps(stage1_payload())}\n```"

    assert extract_json_object(text)["reason"]


def test_surrounding_whitespace_is_tolerated() -> None:
    assert extract_json_object(f"  \n{dumps(stage1_payload())}\n  ")["reason"]


def test_two_fenced_blocks_are_rejected() -> None:
    text = f"{fenced(stage1_payload())}\n{fenced(stage1_payload())}"

    with pytest.raises(ValidationError, match="2 个代码块"):
        extract_json_object(text)


def test_free_text_without_json_is_rejected() -> None:
    with pytest.raises(ValidationError, match="不是完整 JSON 对象"):
        extract_json_object("我认为这条回答是正确的。")


def test_json_with_explanation_prefix_is_rejected() -> None:
    # 前缀文字不解析：不在自由文本里猜答案。
    text = f"分析如下：\n{dumps(stage1_payload())}"

    with pytest.raises(ValidationError):
        extract_json_object(text)


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_output_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError, match="为空"):
        extract_json_object(text)


def test_top_level_array_is_rejected() -> None:
    with pytest.raises(ValidationError, match="顶层必须是 JSON 对象"):
        extract_json_object("```json\n[1, 2]\n```")


def test_bare_array_without_fence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="不是完整 JSON 对象"):
        extract_json_object("[1, 2]")


def test_broken_json_is_rejected() -> None:
    with pytest.raises(ValidationError, match="不是合法 JSON"):
        extract_json_object('{"reason": "缺少右括号"')


def test_empty_fenced_block_is_rejected() -> None:
    with pytest.raises(ValidationError):
        extract_json_object("```json\n\n```")


# ---------------------------------------------------------------- 阶段一解析


def test_valid_stage1_output_parses() -> None:
    analysis = parse_stage1(dumps(stage1_payload()), make_record())

    assert analysis.evidence_sufficiency == "sufficient"
    assert analysis.evidence[0].ref_id == "ref1"


def test_stage1_has_no_business_label_field() -> None:
    # 阶段一不得产出业务标签：结构上就不存在该字段。
    assert "label" not in Stage1Analysis.model_fields


def test_stage1_ignores_extra_keys_without_failing() -> None:
    # 多给的键被忽略；解析结果里不会出现标签。
    analysis = parse_stage1(dumps(stage1_payload(label="回答正确")), make_record())

    assert not hasattr(analysis, "label")
    assert analysis.model_dump().keys() == Stage1Analysis.model_fields.keys()


def test_stage1_evidence_must_be_real_substring() -> None:
    payload = stage1_payload(evidence=[{"ref_id": "ref1", "quote": "需要身份证"}])

    with pytest.raises(ValidationError, match="真实子串"):
        parse_stage1(dumps(payload), make_record())


def test_stage1_evidence_ref_must_be_in_range() -> None:
    payload = stage1_payload(evidence=[{"ref_id": "ref11", "quote": REF1}])

    with pytest.raises(ValidationError, match="ref 编号"):
        parse_stage1(dumps(payload), make_record())


def test_sufficient_without_evidence_is_rejected() -> None:
    payload = stage1_payload(evidence=[], missing_information=["缺少办理时间"])

    with pytest.raises(ValidationError, match="必须有证据"):
        parse_stage1(dumps(payload), make_record())


def test_no_evidence_without_gap_is_rejected() -> None:
    payload = stage1_payload(
        evidence_sufficiency="insufficient", evidence=[], missing_information=[]
    )

    with pytest.raises(ValidationError, match="资料缺口"):
        parse_stage1(dumps(payload), make_record())


def test_stage1_requires_required_points() -> None:
    payload = stage1_payload(required_points=[])

    with pytest.raises(ValidationError, match="核心作答要点"):
        parse_stage1(dumps(payload), make_record())


def test_stage1_requires_reason() -> None:
    payload = stage1_payload(reason="   ")

    with pytest.raises(ValidationError, match="理由"):
        parse_stage1(dumps(payload), make_record())


def test_uncertain_stage1_with_gap_is_allowed() -> None:
    payload = stage1_payload(
        evidence_sufficiency="uncertain",
        evidence=[],
        missing_information=["缺少提问时间"],
    )

    assert parse_stage1(dumps(payload), make_record()).evidence_sufficiency == "uncertain"


def test_internal_sufficiency_values_are_limited() -> None:
    payload = stage1_payload(evidence_sufficiency="部分足够")

    with pytest.raises(ValidationError, match="阶段一字段不合法"):
        parse_stage1(dumps(payload), make_record())


# ---------------------------------------------------------------- 阶段二解析


def test_valid_stage2_output_parses() -> None:
    judgement = parse_stage2(dumps(stage2_payload()), make_record())

    assert judgement.label is Label.CORRECT
    assert judgement.review_required is False


@pytest.mark.parametrize("label", ["正确", "回答正确 ", "", "NO_CORRECT_REF", "部分正确"])
def test_invalid_labels_are_rejected_at_parse(label: str) -> None:
    with pytest.raises(ValidationError, match="阶段二字段不合法"):
        parse_stage2(dumps(stage2_payload(label=label)), make_record())


def test_correct_with_insufficient_evidence_is_rejected() -> None:
    payload = stage2_payload(
        label="回答正确", evidence_sufficiency="insufficient", evidence=[]
    )

    with pytest.raises(ValidationError, match="自相矛盾"):
        parse_stage2(dumps(payload), make_record())


def test_correct_without_evidence_is_rejected() -> None:
    payload = stage2_payload(label="回答正确", evidence=[])

    with pytest.raises(ValidationError, match="必须给出证据"):
        parse_stage2(dumps(payload), make_record())


def test_ref_ok_answer_wrong_with_insufficient_evidence_is_rejected() -> None:
    payload = stage2_payload(
        label="检索到正确资料但回答错误", evidence_sufficiency="insufficient", evidence=[]
    )

    with pytest.raises(ValidationError, match="不能是 insufficient"):
        parse_stage2(dumps(payload), make_record())


def test_no_correct_ref_without_evidence_is_allowed() -> None:
    payload = stage2_payload(
        label="未检索到正确资料", evidence_sufficiency="insufficient", evidence=[]
    )

    assert parse_stage2(dumps(payload), make_record()).label is Label.NO_CORRECT_REF


def test_uncertain_requires_review() -> None:
    payload = stage2_payload(
        label="未检索到正确资料",
        evidence_sufficiency="uncertain",
        review_required=False,
    )

    with pytest.raises(ValidationError, match="必须标记复核"):
        parse_stage2(dumps(payload), make_record())


def test_review_without_reason_is_rejected() -> None:
    payload = stage2_payload(review_required=True, review_reasons=[])

    with pytest.raises(ValidationError, match="复核原因"):
        parse_stage2(dumps(payload), make_record())


def test_stage2_requires_reason() -> None:
    payload = stage2_payload(reason="")

    with pytest.raises(ValidationError, match="判断理由"):
        parse_stage2(dumps(payload), make_record())


def test_correction_affecting_classification_requires_review() -> None:
    payload = stage2_payload(
        review_required=False,
        review_reasons=[],
        stage1_corrections=[
            {
                "corrected_item": "证据充分性：sufficient → insufficient",
                "reason": "只覆盖部分要点",
                "evidence": [{"ref_id": "ref1", "quote": "需携带学生证"}],
                "affects_classification": True,
            }
        ],
    )

    with pytest.raises(ValidationError, match="重大更正必须标记复核"):
        parse_stage2(dumps(payload), make_record())


def test_correction_needs_reason() -> None:
    payload = stage2_payload(
        review_required=True,
        review_reasons=["有更正"],
        stage1_corrections=[
            {
                "corrected_item": "补充要点",
                "reason": "  ",
                "evidence": [],
                "affects_classification": False,
            }
        ],
    )

    with pytest.raises(ValidationError, match="更正必须给出理由"):
        parse_stage2(dumps(payload), make_record())


def test_correction_evidence_is_checked_against_refs() -> None:
    payload = stage2_payload(
        review_required=True,
        review_reasons=["有更正"],
        stage1_corrections=[
            {
                "corrected_item": "证据充分性",
                "reason": "引用有误",
                "evidence": [{"ref_id": "ref2", "quote": "任意"}],
                "affects_classification": False,
            }
        ],
    )

    with pytest.raises(ValidationError, match="空资料"):
        parse_stage2(dumps(payload), make_record())


# ---------------------------------------------------------------- 直接调用一致性检查


def test_consistency_checks_accept_valid_models() -> None:
    assert validate_stage1_consistency(Stage1Analysis.model_validate(stage1_payload()))
    assert validate_stage2_consistency(Stage2Judgement.model_validate(stage2_payload()))


def test_stage1_evidence_normalises_line_endings() -> None:
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs["ref1"] = "第一行\r\n第二行"
    record = make_record(refs=refs)
    payload = stage1_payload(evidence=[{"ref_id": "ref1", "quote": "第一行\n第二行"}])

    assert parse_stage1(dumps(payload), record).evidence[0].quote == "第一行\n第二行"


def test_evidence_model_rejects_unknown_ref_shape() -> None:
    with pytest.raises(ValidationError):
        parse_stage1(dumps(stage1_payload(evidence=[{"quote": "需携带学生证"}])), make_record())


def test_evidence_tuple_type_is_preserved() -> None:
    evidence = Evidence(ref_id="ref1", quote="需携带学生证")

    assert evidence.ref_id == "ref1"
