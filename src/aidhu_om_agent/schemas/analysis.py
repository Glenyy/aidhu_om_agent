"""S03-02：阶段一（资料分析）的数据类型。

阶段一只有 q 与 ref1—ref10 可用，因此这里的字段**不能**包含任何对已有回答
``a`` 的判断。资料充分性 ``evidence_sufficiency`` 是内部分析值，不出现在业务
「判断」列（见 [plan/02 §2](../../../plan/02-架构与详细设计.md)）。

字段名与 [plan/02 §2](../../../plan/02-架构与详细设计.md) 的 ``Stage1Analysis``
一致；阶段二会复用 ``EvidenceSufficiency``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .qa import Evidence

#: 阶段一结构与提示词版本；两者任一变化都必须递增，S04 据此判断快照兼容性。
STAGE1_SCHEMA_VERSION = "1.1"

#: 资料充分性：内部分析值，不是业务标签，不进入导出「判断」列。
EvidenceSufficiency = Literal["sufficient", "insufficient", "uncertain"]


class Stage1Analysis(BaseModel):
    """阶段一的校验后结果。

    ``evidence`` 中每条都必须是某个 ref 的原文子串，由
    `agent/validation.py` 在解析后核对；本类型只描述结构。
    """

    model_config = ConfigDict(frozen=True)

    required_points: tuple[str, ...]
    evidence_sufficiency: EvidenceSufficiency
    evidence: tuple[Evidence, ...] = ()
    missing_information: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    reason: str


__all__ = ["STAGE1_SCHEMA_VERSION", "EvidenceSufficiency", "Stage1Analysis"]
