"""S03-03：阶段二请求构造。

请求包含 **q、a、全部 10 个原始 ref 与通过校验的阶段一结果**；不能只发送阶段一
摘要（见 [plan/02 §3](../../../plan/02-架构与详细设计.md) 与 [plan/01 AC-02](../../../plan/01-需求与验收标准.md)）。

``build_stage2_messages`` 是纯函数，便于自动化核对请求边界；它不调用模型。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from ..prompts import load_prompt
from ..schemas.analysis import Stage1Analysis
from ..schemas.qa import QARecord

#: 与 `schemas/judgement.py::STAGE2_SCHEMA_VERSION` 对应；提示词改动须递增。
#: 1.2（S04-08）：显式区分「逐字」与 JSON 内换行须写 ``\n``，并禁止改写列表符号。
STAGE2_PROMPT_VERSION = "1.2"

_EMPTY_REF = "（本份资料为空）"


def _ref_block(record: QARecord) -> str:
    """按 ref1—ref10 顺序渲染**原始**资料，不用阶段一摘录代替原文。"""
    parts: list[str] = []
    for ref_id, text in record.refs.items():
        body = text if text else _EMPTY_REF
        parts.append(f"[{ref_id}]\n{body}")
    return "\n\n".join(parts)


def _stage1_block(stage1: Stage1Analysis) -> str:
    """完整渲染阶段一结果（不是摘要），使阶段二能复核它。"""
    return json.dumps(stage1.model_dump(mode="json"), ensure_ascii=False, indent=2)


def build_stage2_messages(
    record: QARecord, stage1: Stage1Analysis, *, prompt: str | None = None
) -> list[dict[str, str]]:
    """构造阶段二消息：system 为提示词，user 为 q、a、全部 ref 与阶段一结果。

    有效记录的 ``q``/``a`` 必非空；缺失时抛 ``ValueError``，不做静默填充。

    ``prompt`` 用于传**批次冻结的提示词快照**（S04：恢复与执行都必须用快照而不是
    当前磁盘内容）；为 ``None`` 时读磁盘上的当前版本。
    """
    if record.q is None:
        raise ValueError(f"来源行 {record.source_row} 缺少 q，不能进入阶段二")
    if record.a is None:
        raise ValueError(f"来源行 {record.source_row} 缺少 a，不能进入阶段二")

    user_content = "\n\n".join(
        (
            "【问题 q】",
            record.q,
            "【已有回答 a】",
            record.a,
            "【原始资料 ref1—ref10】",
            _ref_block(record),
            "【阶段一分析结果（已通过校验）】",
            _stage1_block(stage1),
        )
    )
    return [
        {
            "role": "system",
            "content": load_prompt("stage2") if prompt is None else prompt,
        },
        {"role": "user", "content": user_content},
    ]


def stage2_missing_refs(
    messages: list[Mapping[str, str]], record: QARecord
) -> tuple[str, ...]:
    """请求中缺失了哪些**非空**原始 ref 的原文；供审阅与单测断言使用。"""
    joined = "\n".join(str(message.get("content", "")) for message in messages)
    return tuple(
        ref_id for ref_id, text in record.refs.items() if text and text not in joined
    )


__all__ = [
    "STAGE2_PROMPT_VERSION",
    "build_stage2_messages",
    "stage2_missing_refs",
]
