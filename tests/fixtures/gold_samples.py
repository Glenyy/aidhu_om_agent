"""S07-01：合成人工核定样例生成器。

仓库不提交二进制 .xlsx（S02 决定，见 [plan/04](../../../plan/04-测试与质量评估.md)），
因此「入库的合成 gold 样例」是**本模块的代码**：测试与手动指南在运行时按需生成。
全部为合成问答，**不含任何真实数据**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from aidhu_om_agent.evaluation.gold import (
    EXCLUDED_YES,
    GOLD_EXCLUDED_COLUMN,
    GOLD_EXCLUSION_REASON_COLUMN,
    GOLD_JUDGEMENT_COLUMN,
    GOLD_KEY_REF_COLUMN,
    GOLD_REASON_COLUMN,
    META_ANNOTATED_BY,
    META_ANNOTATED_ON,
    META_DATA_VERSION,
    META_SOURCE_SHA256,
    write_gold_workbook,
)
from aidhu_om_agent.schemas.judgement import Label
from aidhu_om_agent.schemas.qa import A_COLUMN, ID_COLUMN, Q_COLUMN, REF_FIELDS

#: 合成样例的原始文件摘要前缀；只是格式合法的占位值，不对应任何真实文件。
SYNTHETIC_SOURCE_SHA256 = "0123456789ab"

METADATA: dict[str, object] = {
    META_DATA_VERSION: "synthetic-v1",
    META_ANNOTATED_BY: "合成样例",
    META_ANNOTATED_ON: "2026-10-07",
    META_SOURCE_SHA256: SYNTHETIC_SOURCE_SHA256,
}


def gold_row(
    record_id: object,
    *,
    judgement: object = None,
    reason: object = None,
    key_ref: object = None,
    excluded: object = None,
    exclusion_reason: object = None,
    q: object = "合成提问：如何办理校园卡？",
    a: object = "合成回答：请到校园卡中心办理。",
    refs: Sequence[object] = ("合成资料：校园卡首次办理需携带身份证，工本费 20 元。",),
) -> dict[str, object]:
    """按列名构造一行核定记录；未给出的列为空。"""
    cells: dict[str, object] = {
        ID_COLUMN: record_id,
        Q_COLUMN: q,
        A_COLUMN: a,
        GOLD_JUDGEMENT_COLUMN: judgement,
        GOLD_REASON_COLUMN: reason,
        GOLD_KEY_REF_COLUMN: key_ref,
        GOLD_EXCLUDED_COLUMN: excluded,
        GOLD_EXCLUSION_REASON_COLUMN: exclusion_reason,
    }
    for field, text in zip(REF_FIELDS, refs):
        cells[field] = text
    return cells


#: 一份合法样例：12 条记录覆盖三个标签与一条人工排除。
#: 三个标签的条数刻意不等（6／3／2，另 1 条排除），便于 S07-02 的划分测试
#: 观察「最大余数分配」而不是恰好整除。
def default_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(1, 7):  # 6 条「回答正确」
        rows.append(
            gold_row(
                f"G-{index}",
                judgement=Label.CORRECT.value,
                reason=f"合成理由 {index}：资料支持回答。",
                key_ref="ref1",
            )
        )
    for index in range(7, 10):  # 3 条「未检索到正确资料」
        rows.append(
            gold_row(
                f"G-{index}",
                judgement=Label.NO_CORRECT_REF.value,
                reason=f"合成理由 {index}：资料与问题无关。",
                refs=(None,),
            )
        )
    for index in range(10, 12):  # 2 条「检索到正确资料但回答错误」
        rows.append(
            gold_row(
                f"G-{index}",
                judgement=Label.REF_OK_ANSWER_WRONG.value,
                reason=f"合成理由 {index}：资料足够但回答与资料冲突。",
                key_ref="ref1、ref2",
                refs=("合成资料一。", "合成资料二。"),
            )
        )
    rows.append(  # 1 条人工排除：标签留空，理由必填
        gold_row(
            "G-12",
            excluded=EXCLUDED_YES,
            exclusion_reason="合成理由：提问超出意图范围。",
        )
    )
    return rows


def write_synthetic_gold(
    path: Path,
    rows: Iterable[Mapping[str, object]] | None = None,
    *,
    metadata: Mapping[str, object] | None = None,
) -> Path:
    """写出合成核定文件并返回路径。"""
    return write_gold_workbook(
        path,
        default_rows() if rows is None else rows,
        METADATA if metadata is None else metadata,
    )
