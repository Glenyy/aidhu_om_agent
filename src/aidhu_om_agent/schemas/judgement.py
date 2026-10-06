"""S02-04／S03-03：固定三分类业务标签与阶段二结果类型。

三个标签字符串与 [plan/01 §3](../../../plan/01-需求与验收标准.md) 完全一致，是本项目
唯一的标签定义处；处理状态、资料充分性与人工复核标记都不是新增业务类别，不得
加入 `Label`。

阶段二结果 `Stage2Judgement` 承载三分类、理由、复核标记与对阶段一的更正。
**阶段一原始输出保留，更正写入 `stage1_corrections`，不覆盖阶段一记录。**
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from .analysis import EvidenceSufficiency
from .qa import Evidence

#: 阶段二结构与提示词版本；两者任一变化都必须递增。
STAGE2_SCHEMA_VERSION = "1.1"


class Label(str, Enum):
    """固定业务标签；字符串值即导出与接口中出现的原文。"""

    CORRECT = "回答正确"
    NO_CORRECT_REF = "未检索到正确资料"
    REF_OK_ANSWER_WRONG = "检索到正确资料但回答错误"


#: 合法标签值，按文档顺序。
LABELS: tuple[str, ...] = tuple(label.value for label in Label)


class Stage1Correction(BaseModel):
    """阶段二对阶段一结论的一条更正；携带原文依据，不覆盖阶段一记录。"""

    model_config = ConfigDict(frozen=True)

    corrected_item: str
    reason: str
    evidence: tuple[Evidence, ...] = ()
    affects_classification: bool


class Stage2Judgement(BaseModel):
    """阶段二的校验后结果。

    ``label`` 用 `Label` 枚举做类型校验：非法或近似写法在解析阶段即被拒绝，
    不会成为最终标签。``evidence`` 的引用位置由 `agent/validation.py` 核对。
    """

    model_config = ConfigDict(frozen=True)

    label: Label
    reason: str
    issues: tuple[str, ...] = ()
    evidence_sufficiency: EvidenceSufficiency
    evidence: tuple[Evidence, ...] = ()
    review_required: bool
    review_reasons: tuple[str, ...] = ()
    stage1_corrections: tuple[Stage1Correction, ...] = ()


__all__ = [
    "STAGE2_SCHEMA_VERSION",
    "Label",
    "LABELS",
    "Stage1Correction",
    "Stage2Judgement",
]
