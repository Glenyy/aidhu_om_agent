"""S03-02：阶段一请求构造。

请求**只包含 q 与 ref1—ref10**。不发送 ``a``、不发送整个记录对象、不带历史
回答（见 [plan/02 §3](../../../plan/02-架构与详细设计.md) 与 [plan/01 AC-02](../../../plan/01-需求与验收标准.md)）。

``build_stage1_messages`` 是纯函数，便于自动化核对请求边界；它不调用模型。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..prompts import load_prompt
from ..schemas.qa import QARecord

#: 与 `schemas/analysis.py::STAGE1_SCHEMA_VERSION` 对应；提示词改动须递增。
STAGE1_PROMPT_VERSION = "1.1"

_EMPTY_REF = "（本份资料为空）"


def _ref_block(record: QARecord) -> str:
    """按 ref1—ref10 顺序渲染资料；空资料显式标注，不省略编号。"""
    parts: list[str] = []
    for ref_id, text in record.refs.items():
        body = text if text else _EMPTY_REF
        parts.append(f"[{ref_id}]\n{body}")
    return "\n\n".join(parts)


def build_stage1_messages(record: QARecord) -> list[dict[str, str]]:
    """构造阶段一消息：system 为提示词，user 为 q 与全部 ref。

    有效记录的 ``q`` 必非空；缺失时抛 ``ValueError``，不做静默填充。
    """
    if record.q is None:
        raise ValueError(f"来源行 {record.source_row} 缺少 q，不能进入阶段一")

    user_content = "\n\n".join(
        (
            "【问题 q】",
            record.q,
            "【资料 ref1—ref10】",
            _ref_block(record),
        )
    )
    return [
        {"role": "system", "content": load_prompt("stage1")},
        {"role": "user", "content": user_content},
    ]


def stage1_messages_contain_a(messages: list[Mapping[str, str]], record: QARecord) -> bool:
    """核对请求中是否混入了回答 ``a`` 的文本；供自动化审阅与单测断言使用。"""
    answer = record.a
    if not answer:
        return False
    return any(answer in str(message.get("content", "")) for message in messages)


__all__ = [
    "STAGE1_PROMPT_VERSION",
    "build_stage1_messages",
    "stage1_messages_contain_a",
]
