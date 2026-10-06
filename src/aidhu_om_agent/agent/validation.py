"""S02-04／S03-04：模型输出解析与结构、枚举、引用位置校验。

本模块只校验**结构、枚举和摘录位置**：标签是否合法、ref 编号是否在 ref1—ref10
内、摘录是否是对应原文的真实子串、字段之间是否明显自相矛盾。

它**不能**证明证据语义充分，也不能证明业务判断正确；资料相关不等于充分，必须由
提示词与人工复核处理。

解析规则（[plan/02 §3](../../../plan/02-架构与详细设计.md)）：只接受**完整 JSON
对象**或**单个完整 JSON 代码块**；不在自由文本、推理内容或多个候选对象中猜测答案。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ..schemas.analysis import Stage1Analysis
from ..schemas.judgement import LABELS, Label, Stage2Judgement
from ..schemas.qa import (
    ID_COLUMN,
    REF_COUNT,
    REF_FIELDS,
    Evidence,
    QARecord,
    missing_required_fields,
    normalize_text,
)


class ValidationError(ValueError):
    """结构、枚举、引用位置或一致性问题。

    ``kind`` 是给重试反馈用的**分类标签**（可选、默认 ``None``），只决定回传给
    模型的纠错提示措辞，**不参与任何判定**：判定结果只取决于是否抛出本异常。
    """

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


#: 模型输出校验问题的分类；供 `agent/pipeline.py` 选择重试反馈措辞。
#: 取值稳定，不要随意改名——重试反馈的映射表按这些字符串索引。
KIND_OUTPUT_FORMAT = "output_format"
KIND_FIELD_SCHEMA = "field_schema"
KIND_LABEL_ENUM = "label_enum"
KIND_REF_RANGE = "ref_range"
KIND_EMPTY_REF = "empty_ref"
KIND_EMPTY_QUOTE = "empty_quote"
KIND_EVIDENCE_SUBSTRING = "evidence_substring"
KIND_CONSISTENCY = "consistency"


# --------------------------------------------------------------------------
# 结构与引用位置（S02-04）
# --------------------------------------------------------------------------


def validate_label(value: object) -> str:
    """只接受三个合法中文字符串；空值、近似写法与未知标签一律拒绝。"""
    if not isinstance(value, str):
        raise ValidationError(
            f"判断必须是字符串，实际为 {type(value).__name__}", kind=KIND_FIELD_SCHEMA
        )
    if value not in LABELS:
        allowed = "、".join(LABELS)
        raise ValidationError(
            f"判断只允许 {allowed} 之一，实际为 {value!r}", kind=KIND_LABEL_ENUM
        )
    return value


def validate_evidence(evidence: Evidence, refs: Mapping[str, str | None]) -> Evidence:
    """核对 ref 编号范围、非空摘录与摘录位置。

    摘录在统一换行并去首尾空白后，必须是对应 ref 原文的真实子串；
    改写标点或拼接出不存在的摘录都会被拒绝。
    """
    ref_id = evidence.ref_id
    if ref_id not in REF_FIELDS:
        raise ValidationError(
            f"证据 ref 编号必须是 {REF_FIELDS[0]}—{REF_FIELDS[-1]} 之一，实际为 {ref_id!r}",
            kind=KIND_REF_RANGE,
        )

    source = refs.get(ref_id)
    if source is None:
        raise ValidationError(
            f"证据引用了空资料 {ref_id}，没有原文可供核对", kind=KIND_EMPTY_REF
        )

    quote = normalize_text(evidence.quote)
    if not quote:
        raise ValidationError("证据摘录不能为空", kind=KIND_EMPTY_QUOTE)
    if quote not in normalize_text(source):
        raise ValidationError(
            f"证据摘录不是 {ref_id} 原文的真实子串", kind=KIND_EVIDENCE_SUBSTRING
        )
    return evidence


def validate_evidence_list(
    evidences: Iterable[Evidence], refs: Mapping[str, str | None]
) -> tuple[Evidence, ...]:
    """逐条校验证据，任一条失败即整体拒绝。"""
    return tuple(validate_evidence(evidence, refs) for evidence in evidences)


def validate_record_structure(record: QARecord) -> QARecord:
    """有效记录必须有编号、q 与 a；ref 为空合法，不算结构问题。"""
    missing = missing_required_fields(record)
    if missing:
        raise ValidationError(
            f"来源行 {record.source_row} 的有效记录缺少必填列：{'、'.join(missing)}"
        )
    keys = tuple(record.refs)
    if keys != REF_FIELDS:
        raise ValidationError(
            f"来源行 {record.source_row} 的 refs 必须包含 {REF_COUNT} 项 "
            f"{REF_FIELDS[0]}—{REF_FIELDS[-1]}"
        )
    return record


# --------------------------------------------------------------------------
# 模型输出解析（S03-04）
# --------------------------------------------------------------------------

#: 单个围栏代码块；正文允许为空行，非贪婪以配合“只允许一块”的判定。
_FENCE_BLOCK = re.compile(r"```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n?```", re.DOTALL)

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_MAX_REPORTED_ERRORS = 3


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出中取出唯一的 JSON 对象。

    接受两种形式：整段就是一个完整 JSON 对象，或整段**有且仅有一个**完整的
    JSON 代码块。多个代码块、无代码块且不是裸 JSON 对象、以及代码块外无法解析
    的文本一律拒绝——不猜答案。
    """
    if not isinstance(text, str):
        raise ValidationError("模型输出必须是文本", kind=KIND_OUTPUT_FORMAT)
    stripped = text.strip()
    if not stripped:
        raise ValidationError("模型输出为空，没有可解析的内容", kind=KIND_OUTPUT_FORMAT)

    blocks = _FENCE_BLOCK.findall(stripped)
    if len(blocks) > 1:
        raise ValidationError(
            f"模型输出包含 {len(blocks)} 个代码块，无法确定唯一结果",
            kind=KIND_OUTPUT_FORMAT,
        )

    if blocks:
        candidate = blocks[0].strip()
    else:
        if not stripped.startswith("{"):
            raise ValidationError(
                "模型输出既不是完整 JSON 对象，也不含单个完整 JSON 代码块",
                kind=KIND_OUTPUT_FORMAT,
            )
        candidate = stripped

    if not candidate:
        raise ValidationError("JSON 代码块内容为空", kind=KIND_OUTPUT_FORMAT)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"模型输出不是合法 JSON：{exc.msg}（位置 {exc.pos}）",
            kind=KIND_OUTPUT_FORMAT,
        ) from exc
    if not isinstance(value, dict):
        raise ValidationError(
            f"模型输出的顶层必须是 JSON 对象，实际为 {type(value).__name__}",
            kind=KIND_OUTPUT_FORMAT,
        )
    return value


def _shorten_pydantic_error(exc: PydanticValidationError) -> str:
    """把 Pydantic 错误压成简短、可回传模型的说明。"""
    parts = []
    for item in exc.errors()[:_MAX_REPORTED_ERRORS]:
        location = ".".join(str(piece) for piece in item.get("loc", ())) or "顶层"
        parts.append(f"{location}：{item.get('msg', '校验失败')}")
    remaining = len(exc.errors()) - len(parts)
    if remaining > 0:
        parts.append(f"另有 {remaining} 处问题")
    return "；".join(parts)


def _model_from_json(text: str, model_cls: type[_ModelT], what: str) -> _ModelT:
    payload = extract_json_object(text)
    try:
        return model_cls.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"{what}字段不合法——{_shorten_pydantic_error(exc)}", kind=KIND_FIELD_SCHEMA
        ) from exc


def validate_stage1_consistency(analysis: Stage1Analysis) -> Stage1Analysis:
    """阶段一字段间的明显矛盾检查；语义充分性不在程序可判定范围内。"""
    if not analysis.required_points:
        raise ValidationError(
            "阶段一必须给出至少一条核心作答要点", kind=KIND_CONSISTENCY
        )
    if not normalize_text(analysis.reason):
        raise ValidationError("阶段一必须给出充分性判断的理由", kind=KIND_CONSISTENCY)
    if analysis.evidence_sufficiency == "sufficient" and not analysis.evidence:
        raise ValidationError("充分性为 sufficient 时必须有证据", kind=KIND_CONSISTENCY)
    if not analysis.evidence and not analysis.missing_information:
        raise ValidationError("没有证据时必须说明资料缺口", kind=KIND_CONSISTENCY)
    return analysis


def parse_stage1(text: str, record: QARecord) -> Stage1Analysis:
    """解析并校验阶段一输出；任一步失败抛 `ValidationError`。"""
    analysis = _model_from_json(text, Stage1Analysis, "阶段一")
    validate_evidence_list(analysis.evidence, record.refs)
    return validate_stage1_consistency(analysis)


def validate_stage2_consistency(judgement: Stage2Judgement) -> Stage2Judgement:
    """阶段二字段间的明显矛盾检查。

    只拒绝**定义上互斥**的组合，避免把可讨论的业务口径写成硬校验。
    """
    if not normalize_text(judgement.reason):
        raise ValidationError("阶段二必须给出判断理由", kind=KIND_CONSISTENCY)
    if judgement.review_required and not judgement.review_reasons:
        raise ValidationError("标记需复核时必须给出复核原因", kind=KIND_CONSISTENCY)
    if judgement.evidence_sufficiency == "uncertain" and not judgement.review_required:
        raise ValidationError("充分性为 uncertain 时必须标记复核", kind=KIND_CONSISTENCY)

    for correction in judgement.stage1_corrections:
        if not normalize_text(correction.reason):
            raise ValidationError("阶段一对更正必须给出理由", kind=KIND_CONSISTENCY)
        if correction.affects_classification and not judgement.review_required:
            raise ValidationError(
                "影响最终分类的重大更正必须标记复核", kind=KIND_CONSISTENCY
            )

    if judgement.label == Label.CORRECT:
        if judgement.evidence_sufficiency == "insufficient":
            raise ValidationError("资料不足却判为回答正确，明显自相矛盾", kind=KIND_CONSISTENCY)
        if not judgement.evidence:
            raise ValidationError("判为回答正确必须给出证据", kind=KIND_CONSISTENCY)

    if (
        judgement.label == Label.REF_OK_ANSWER_WRONG
        and judgement.evidence_sufficiency == "insufficient"
    ):
        raise ValidationError(
            "判为检索到正确资料但回答错误，充分性不能是 insufficient",
            kind=KIND_CONSISTENCY,
        )

    return judgement


def parse_stage2(text: str, record: QARecord) -> Stage2Judgement:
    """解析并校验阶段二输出；任一步失败抛 `ValidationError`。"""
    judgement = _model_from_json(text, Stage2Judgement, "阶段二")
    validate_evidence_list(judgement.evidence, record.refs)
    for correction in judgement.stage1_corrections:
        validate_evidence_list(correction.evidence, record.refs)
    return validate_stage2_consistency(judgement)


__all__ = [
    "ValidationError",
    "KIND_OUTPUT_FORMAT",
    "KIND_FIELD_SCHEMA",
    "KIND_LABEL_ENUM",
    "KIND_REF_RANGE",
    "KIND_EMPTY_REF",
    "KIND_EMPTY_QUOTE",
    "KIND_EVIDENCE_SUBSTRING",
    "KIND_CONSISTENCY",
    "validate_label",
    "validate_evidence",
    "validate_evidence_list",
    "validate_record_structure",
    "extract_json_object",
    "validate_stage1_consistency",
    "validate_stage2_consistency",
    "parse_stage1",
    "parse_stage2",
    "missing_required_fields",
    "ID_COLUMN",
]
