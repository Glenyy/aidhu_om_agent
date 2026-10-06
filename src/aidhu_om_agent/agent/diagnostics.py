"""S03 返工：摘录与原文的**字符级**诊断。

只报告可核对的事实——是否子串、差在哪个字符、最长公共子串多长、相似度多少、
原文对应位置长什么样。**不做任何推测**：不写“模型大概把 `**` 去掉了”这类结论，
那属于人（或后续分析）的工作。

本模块不参与判定：判定仍只看 `agent/validation.py` 的逐字子串规则。
"""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any

from ..schemas.qa import normalize_text
from .validation import ValidationError, extract_json_object

#: 相似度计算的最长参与长度；超长时只取前若干字符，避免诊断本身变慢。
_SIMILARITY_LIMIT = 4000

#: 原文窗口在最长公共子串两侧额外展示的字符数。
_WINDOW_PAD = 30

#: 最长公共子串回显的字符上限。
_LCS_ECHO_LIMIT = 200


def _longest_match(quote: str, source: str) -> tuple[int, int, int]:
    """返回 (quote 起始, source 起始, 长度)，即最长公共子串的位置。"""
    if not quote or not source:
        return 0, 0, 0
    best = (0, 0, 0)
    matcher = SequenceMatcher(None, quote, source, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size > best[2]:
            best = (block.a, block.b, block.size)
    return best


def diagnose_quote(
    quote: str, ref_id: str, refs: Mapping[str, str | None]
) -> dict[str, Any]:
    """核对一条证据摘录与对应 ref 原文的差异，返回事实字段。

    ``refs`` 是记录的 ref 映射；``ref_id`` 不在其中或原文为空时，
    ``ref_available`` 为 ``False``，其余字段为 ``None``。
    """
    source = refs.get(ref_id)
    normalized_quote = normalize_text(quote)
    if source is None or not normalize_text(source):
        return {
            "ref_id": ref_id,
            "ref_available": False,
            "is_substring": False,
            "quote_len": len(normalized_quote),
            "source_len": 0 if source is None else len(normalize_text(source)),
            "longest_common_substring_len": None,
            "longest_common_substring": None,
            "similarity_ratio": None,
            "first_divergence_index": None,
            "first_divergence": None,
            "source_window": None,
        }

    normalized_source = normalize_text(source)
    is_substring = normalized_quote in normalized_source
    qa, sb, size = _longest_match(normalized_quote, normalized_source)

    similarity = SequenceMatcher(
        None,
        normalized_quote[:_SIMILARITY_LIMIT],
        normalized_source[:_SIMILARITY_LIMIT],
        autojunk=False,
    ).ratio()

    divergence_index: int | None = None
    divergence: dict[str, Any] | None = None
    if not is_substring:
        # 从最长公共子串之后逐字符前进，第一个不相等的位置就是分歧点。
        i, j = qa + size, sb + size
        while i < len(normalized_quote) and j < len(normalized_source):
            if normalized_quote[i] != normalized_source[j]:
                break
            i += 1
            j += 1
        divergence_index = i
        divergence = {
            "index_in_quote": i,
            "quote_char": normalized_quote[i] if i < len(normalized_quote) else None,
            "source_char": normalized_source[j] if j < len(normalized_source) else None,
        }

    window_start = max(0, sb - _WINDOW_PAD)
    window_end = min(len(normalized_source), sb + size + _WINDOW_PAD)
    return {
        "ref_id": ref_id,
        "ref_available": True,
        "is_substring": is_substring,
        "quote_len": len(normalized_quote),
        "source_len": len(normalized_source),
        "longest_common_substring_len": size,
        "longest_common_substring": normalized_quote[qa : qa + size][:_LCS_ECHO_LIMIT],
        "similarity_ratio": round(similarity, 4),
        "first_divergence_index": divergence_index,
        "first_divergence": divergence,
        "source_window": normalized_source[window_start:window_end],
    }


def diagnose_evidences(
    evidences: object, refs: Mapping[str, str | None]
) -> list[dict[str, Any]]:
    """对一组证据对象（含 ``ref_id``/``quote``）逐条诊断；字段缺失时跳过。"""
    if not isinstance(evidences, (list, tuple)):
        return []
    results = []
    for evidence in evidences:
        ref_id = getattr(evidence, "ref_id", None)
        quote = getattr(evidence, "quote", None)
        if isinstance(ref_id, str) and isinstance(quote, str):
            results.append(diagnose_quote(quote, ref_id, refs))
    return results


def _evidence_items(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """从模型输出对象里取出 (ref_id, quote) 对；阶段一更正里的证据也算。"""
    items: list[tuple[str, str]] = []

    def collect(container: Mapping[str, Any]) -> None:
        evidences = container.get("evidence")
        if not isinstance(evidences, list):
            return
        for evidence in evidences:
            if not isinstance(evidence, Mapping):
                continue
            ref_id = evidence.get("ref_id")
            quote = evidence.get("quote")
            if isinstance(ref_id, str) and isinstance(quote, str):
                items.append((ref_id, quote))

    collect(payload)
    corrections = payload.get("stage1_corrections")
    if isinstance(corrections, list):
        for correction in corrections:
            if isinstance(correction, Mapping):
                collect(correction)
    return items


def diagnose_raw_output(text: str, refs: Mapping[str, str | None]) -> dict[str, Any]:
    """尽力从**被拒的原始输出**里取出证据摘录逐条诊断。

    取不到 JSON 时如实说明原因（``parseable`` 为 ``False``），不猜内容。
    """
    try:
        payload = extract_json_object(text)
    except ValidationError as exc:
        return {"parseable": False, "note": str(exc), "quotes": []}
    return {
        "parseable": True,
        "note": None,
        "quotes": [diagnose_quote(quote, ref_id, refs) for ref_id, quote in _evidence_items(payload)],
    }


__all__ = ["diagnose_evidences", "diagnose_quote", "diagnose_raw_output"]
