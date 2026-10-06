"""S02-04 的标签枚举、记录结构与引用位置校验单测。

覆盖：合法标签通过、空值与未知标签拒绝、摘录必须是原文真实子串、
换行差异归一化后一致、伪造摘录/跨 ref 错配/空摘录/越界 ref 编号拒绝。
"""

from __future__ import annotations

import pytest

from aidhu_om_agent.agent.validation import (
    KIND_EMPTY_QUOTE,
    KIND_EMPTY_REF,
    KIND_EVIDENCE_SUBSTRING,
    KIND_FIELD_SCHEMA,
    KIND_LABEL_ENUM,
    KIND_OUTPUT_FORMAT,
    KIND_REF_RANGE,
    KIND_CONSISTENCY,
    ValidationError,
    validate_evidence,
    validate_evidence_list,
    validate_label,
    validate_record_structure,
)
from aidhu_om_agent.schemas.judgement import LABELS, Label
from aidhu_om_agent.schemas.qa import REF_FIELDS, Evidence, QARecord

REFS: dict[str, str | None] = {
    "ref1": "校园卡补办需携带学生证，到校园卡中心办理。",
    "ref2": "办理时间为工作日 9:00—17:00。",
    "ref3": None,
    **{field: None for field in REF_FIELDS[3:]},
}


def make_record(**overrides: object) -> QARecord:
    data: dict[str, object] = {
        "record_id": "1",
        "source_row": 2,
        "order_index": 0,
        "q": "如何补办校园卡？",
        "a": "带学生证去校园卡中心。",
        "refs": dict(REFS),
    }
    data.update(overrides)
    return QARecord(**data)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 标签


def test_labels_match_requirement_document() -> None:
    assert LABELS == ("回答正确", "未检索到正确资料", "检索到正确资料但回答错误")
    assert Label.CORRECT.value == "回答正确"


@pytest.mark.parametrize("label", LABELS)
def test_valid_labels_pass(label: str) -> None:
    assert validate_label(label) == label


@pytest.mark.parametrize(
    "value",
    ["", " ", "回答正确 ", "正确", "unknown", "NO_CORRECT_REF", None, 0, True],
)
def test_invalid_labels_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        validate_label(value)


def test_non_string_label_names_the_type() -> None:
    with pytest.raises(ValidationError, match="必须是字符串"):
        validate_label(3)


# ---------------------------------------------------------------- 引用位置


def test_real_substring_passes() -> None:
    evidence = Evidence(ref_id="ref1", quote="需携带学生证")

    assert validate_evidence(evidence, REFS) is evidence


def test_newline_and_whitespace_difference_still_passes() -> None:
    refs = {**REFS, "ref1": "第一行\r\n第二行"}
    evidence = Evidence(ref_id="ref1", quote="  第一行\n第二行  ")

    assert validate_evidence(evidence, refs) is evidence


def test_rewritten_punctuation_is_rejected() -> None:
    evidence = Evidence(ref_id="ref1", quote="校园卡补办需携带学生证, 到校园卡中心办理。")

    with pytest.raises(ValidationError, match="真实子串"):
        validate_evidence(evidence, REFS)


def test_stitched_quote_across_refs_is_rejected() -> None:
    evidence = Evidence(ref_id="ref1", quote="校园卡补办需携带学生证，到校园卡中心办理。办理时间为工作日")

    with pytest.raises(ValidationError, match="真实子串"):
        validate_evidence(evidence, REFS)


def test_quote_from_another_ref_is_rejected() -> None:
    evidence = Evidence(ref_id="ref1", quote="办理时间为工作日 9:00—17:00。")

    with pytest.raises(ValidationError, match="真实子串"):
        validate_evidence(evidence, REFS)


@pytest.mark.parametrize("quote", ["", "   ", "\n"])
def test_empty_quote_is_rejected(quote: str) -> None:
    with pytest.raises(ValidationError, match="不能为空"):
        validate_evidence(Evidence(ref_id="ref1", quote=quote), REFS)


@pytest.mark.parametrize("ref_id", ["ref0", "ref11", "ref", "q", "", "REF1"])
def test_out_of_range_ref_id_is_rejected(ref_id: str) -> None:
    with pytest.raises(ValidationError, match="ref 编号"):
        validate_evidence(Evidence(ref_id=ref_id, quote="任意"), REFS)


def test_quote_against_empty_ref_is_rejected() -> None:
    evidence = Evidence(ref_id="ref3", quote="任意摘录")

    with pytest.raises(ValidationError, match="空资料"):
        validate_evidence(evidence, REFS)


def test_evidence_list_rejects_if_any_item_fails() -> None:
    evidences = [Evidence(ref_id="ref1", quote="需携带学生证"), Evidence(ref_id="ref2", quote="不存在")]

    with pytest.raises(ValidationError):
        validate_evidence_list(evidences, REFS)


# ---------------------------------------------------------------- 记录结构


def test_complete_record_passes_structure_check() -> None:
    assert validate_record_structure(make_record()).record_id == "1"


def test_record_without_refs_still_passes_structure_check() -> None:
    # ref 全空是合法的：资料不足属业务判断，不是结构问题。
    record = make_record(refs={field: None for field in REF_FIELDS})

    assert validate_record_structure(record).record_id == "1"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"record_id": None}, "编号"),
        ({"q": None}, "q"),
        ({"a": None}, "a"),
    ],
)
def test_record_missing_required_column_is_rejected(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(ValidationError, match=expected):
        validate_record_structure(make_record(**overrides))


def test_record_rejects_incomplete_refs() -> None:
    with pytest.raises(ValueError, match="refs"):
        make_record(refs={"ref1": "只有一项"})


# --------------------------------------- 2026-10-06 返工：校验问题的分类标签


def test_error_kinds_are_stable_strings() -> None:
    """分类标签只用于选重试措辞，必须稳定：改名会静默改变反馈文案。"""
    kinds = {
        KIND_OUTPUT_FORMAT: "output_format",
        KIND_FIELD_SCHEMA: "field_schema",
        KIND_LABEL_ENUM: "label_enum",
        KIND_REF_RANGE: "ref_range",
        KIND_EMPTY_REF: "empty_ref",
        KIND_EMPTY_QUOTE: "empty_quote",
        KIND_EVIDENCE_SUBSTRING: "evidence_substring",
        KIND_CONSISTENCY: "consistency",
    }

    assert all(actual == expected for actual, expected in kinds.items())


def test_substring_failure_is_tagged() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_evidence(Evidence(ref_id="ref1", quote="这段摘录不存在"), REFS)

    assert excinfo.value.kind == KIND_EVIDENCE_SUBSTRING


def test_unknown_label_is_tagged() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_label("部分正确")

    assert excinfo.value.kind == KIND_LABEL_ENUM


def test_empty_quote_and_empty_ref_are_tagged() -> None:
    with pytest.raises(ValidationError) as empty_quote:
        validate_evidence(Evidence(ref_id="ref1", quote="   "), REFS)
    with pytest.raises(ValidationError) as empty_ref:
        validate_evidence(Evidence(ref_id="ref3", quote="随便写点什么"), REFS)

    assert empty_quote.value.kind == KIND_EMPTY_QUOTE
    assert empty_ref.value.kind == KIND_EMPTY_REF


def test_error_kind_defaults_to_none() -> None:
    """未打标的调用点保持原样：kind 不参与任何判定。"""
    error = ValidationError("某条旧消息")

    assert error.kind is None
    assert str(error) == "某条旧消息"
