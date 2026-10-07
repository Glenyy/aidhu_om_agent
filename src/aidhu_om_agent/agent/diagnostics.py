"""S03 返工：摘录与原文的**字符级**诊断。

只报告可核对的事实——是否子串、差在哪个字符、最长公共子串多长、相似度多少、
原文对应位置长什么样。**不做任何推测**：不写“模型大概把 `**` 去掉了”这类结论，
那属于人（或后续分析）的工作。

本模块不参与判定：判定仍只看 `agent/validation.py` 的逐字子串规则。

S06-02 起，原本挂在临时接口内存注册表上的**诊断产物写入**（`_write_diagnostic`）
也搬到这里，由 worker 的记录失败路径调用（S06 阶段文档 §0.2 第 6 项）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..schemas.analysis import STAGE1_SCHEMA_VERSION
from ..schemas.judgement import STAGE2_SCHEMA_VERSION
from ..schemas.qa import QARecord, normalize_text
from .stage1 import STAGE1_PROMPT_VERSION
from .stage2 import STAGE2_PROMPT_VERSION
from .validation import ValidationError, extract_json_object

logger = logging.getLogger(__name__)

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


def needs_diagnostic(payload: Mapping[str, Any]) -> bool:
    """这次记录执行是否值得写诊断产物。

    判定口径与 S03 完全相同：**干净成功不写**（状态 ``completed`` 且每次尝试都是
    ``ok``），其余都写——包括「某次尝试被拒、重试后成功」这种状态为 ``completed``
    但仍有原文可判读的记录。
    """
    attempts = payload.get("attempts") or ()
    if payload.get("status") == "completed" and not any(
        attempt.get("outcome") != "ok" for attempt in attempts
    ):
        return False
    return True


def write_record_diagnostic(
    *,
    runtime_root: str | Path,
    job_id: str,
    run_id: str,
    record_key: str,
    record: QARecord,
    payload: Mapping[str, Any],
    mode: str,
    written_at: str,
) -> Path | None:
    """把一次值得判读的记录执行写到 ``<runtime>/judge-diagnostics/``。

    ``payload`` 用 `agent.pipeline.result_to_payload()` 的形状（与记录详情接口同源），
    所以落盘的内容与界面上看到的是同一份事实。文件名
    ``judge-<job_id>-<record_key>.json``：一个批次任务可能有多条失败记录，
    只写 ``job_id`` 会互相覆盖。

    返回文件路径；不需要写（干净成功）或写失败时返回 ``None``。**整段包 try/except**：
    诊断是观测手段，写盘失败绝不能改变任务状态，也不能把已完成的记录标成失败。
    """
    try:
        if not needs_diagnostic(payload):
            return None

        attempts = payload.get("attempts") or ()
        refs = record.refs or {}
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
            "job_id": job_id,
            "run_id": run_id,
            "record_key": record_key,
            "record_id": payload.get("record_id"),
            "source_row": payload.get("source_row"),
            # worker 的**实际**模式（mock/real），不是 `jobs.mode`（那个是
            # initial/resume/retry_failed，见记录详情接口的同一提醒）。
            "mode": mode,
            # 只写**记录**状态：任务状态要到 `_finalize` 才定，这里写出来就是假事实。
            "record_status": payload.get("status"),
            "written_at": written_at,
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
            "attempts": diagnosed,
        }

        directory = Path(runtime_root) / "judge-diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"judge-{job_id}-{record_key}.json"
        target.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target
    except Exception:  # noqa: BLE001 - 观测失败不得影响判定结果
        logger.warning(
            "诊断产物写入失败（不影响任务状态）：job=%s record=%s", job_id, record_key
        )
        return None


__all__ = [
    "diagnose_evidences",
    "diagnose_quote",
    "diagnose_raw_output",
    "needs_diagnostic",
    "write_record_diagnostic",
]

