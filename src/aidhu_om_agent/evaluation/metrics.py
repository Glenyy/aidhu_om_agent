"""S07-03：混淆矩阵与分类指标（[plan/04 §5—§6](../../../plan/04-测试与质量评估.md)）。

口径逐条对齐 plan/04 §5：

- 类别顺序固定为三个业务标签；**混淆矩阵行 = 人工真值，列 = agent 预测**。
- 每类报 TP／FP／FN／精确率／召回率／F1；Macro-F1 是三个 F1 的**算术平均**。
- 非正确类误判正确率 = 人工非「回答正确」且预测为「回答正确」的数量 ÷ 人工非
  「回答正确」的数量。
- 需复核记录**参与**评分、不剔除；**人工修订不计作 agent 预测**（评分一律用
  `records.final_label`，即 agent 原预测）。
- 每项指标同时给出分子与分母；**分母为 0 记「不可计算」，不记通过**。
- 缺失预测与技术失败**单独报告**，不进混淆矩阵；有效记录未全部完成时总体验收
  不得通过（由「分类完成率」这一项体现）。

一个口径细节：plan/04 的两个分子分母都写在「参与分类评分的记录」上，而
「非正确类误判正确率」的分母「人工标签非回答正确的数量」同样限在这批记录里
——没有预测的记录谈不上「被误判成正确」，它们由完成率那一项把关。报告里同时
给出**有效记录总数**，两个口径都看得见。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..schemas.judgement import LABELS, Label

#: 达标门槛（[plan/04 §6](../../../plan/04-测试与质量评估.md)）。
MAX_WRONG_AS_CORRECT = 0.05
MIN_MACRO_F1 = 0.80
MIN_CLASS_SUPPORT = 5

#: 显示小数位；计算本身不做四舍五入。
DISPLAY_DIGITS = 4


@dataclass(frozen=True)
class MetricValue:
    """一个带分子分母的指标；**分母为 0 即不可计算**，不当作 0 或通过。"""

    numerator: int
    denominator: int

    @property
    def computable(self) -> bool:
        return self.denominator > 0

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.computable else None

    def reduced(self) -> "MetricValue":
        """约到最简分数；**数值不变**，只是让分子分母在人眼里有意义。

        逐类指标本来就是「计数之比」（2·TP / (2·TP+FP+FN)），保持原样最好读；
        需要约分的是分数相加得到的分母（Macro-F1 的分母是三个分母之积）。
        """
        divisor = math.gcd(self.numerator, self.denominator)
        if divisor <= 1:
            return self
        return MetricValue(self.numerator // divisor, self.denominator // divisor)

    def display(self, digits: int = DISPLAY_DIGITS) -> str:
        if not self.computable:
            return "不可计算（分母为 0）"
        return f"{(self.numerator / self.denominator):.{digits}f}（{self.numerator}/{self.denominator}）"

    def as_dict(self) -> dict[str, object]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": self.value,
            "computable": self.computable,
            "display": self.display(),
        }


@dataclass(frozen=True)
class ScoredRecord:
    """参与评估的一条记录；``agent_label`` 为 ``None`` 表示没有合法预测。"""

    record_id: str
    gold_label: str
    agent_label: str | None
    review_required: bool | None
    status: str
    #: 失败阶段；``None`` 表示没有失败记录。用于把「技术失败」与「未跑完」分开报。
    failure_stage: str | None = None


@dataclass(frozen=True)
class ClassMetrics:
    """单类指标；``support`` 是该类**人工标签**的条数（召回率的分母）。"""

    label: str
    tp: int
    fp: int
    fn: int
    support: int
    precision: MetricValue
    recall: MetricValue
    f1: MetricValue

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "support": self.support,
            "precision": self.precision.as_dict(),
            "recall": self.recall.as_dict(),
            "f1": self.f1.as_dict(),
        }


@dataclass(frozen=True)
class ThresholdCheck:
    """一条达标判定；``passed`` 为 ``None`` 表示不可判定（分母为 0）。"""

    key: str
    name: str
    requirement: str
    actual: str
    passed: bool | None

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "requirement": self.requirement,
            "actual": self.actual,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class EvaluationMetrics:
    """一次评估的全部指标；每个数值都能追到分子分母。"""

    scored: tuple[ScoredRecord, ...]
    excluded_ids: tuple[str, ...]
    valid_total: int
    confusion: Mapping[str, Mapping[str, int]]
    per_class: Mapping[str, ClassMetrics]
    macro_f1: MetricValue
    agreement: MetricValue
    wrong_as_correct: MetricValue
    completion: MetricValue
    review_ratio: MetricValue
    missing_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    unfinished_ids: tuple[str, ...]

    @property
    def scored_total(self) -> int:
        return len(self.scored)

    def as_dict(self) -> dict[str, object]:
        return {
            "valid_total": self.valid_total,
            "scored_total": self.scored_total,
            "excluded_total": len(self.excluded_ids),
            "excluded_ids": list(self.excluded_ids),
            "confusion": {row: dict(cells) for row, cells in self.confusion.items()},
            "per_class": {label: cell.as_dict() for label, cell in self.per_class.items()},
            "macro_f1": self.macro_f1.as_dict(),
            "agreement": self.agreement.as_dict(),
            "wrong_as_correct": self.wrong_as_correct.as_dict(),
            "completion": self.completion.as_dict(),
            "review_ratio": self.review_ratio.as_dict(),
            "missing_ids": list(self.missing_ids),
            "failed_ids": list(self.failed_ids),
            "unfinished_ids": list(self.unfinished_ids),
        }


def _is_legal_label(value: str | None) -> bool:
    return value is not None and value in LABELS


def compute_metrics(
    records: Iterable[ScoredRecord], *, excluded_ids: Sequence[str] = ()
) -> EvaluationMetrics:
    """按 plan/04 §5 计算全部指标。

    ``records`` 应包含**全部人工核定且未排除的有效记录**（有预测的与没预测的都
    放进来）：没预测的会进 `missing_ids` 并拉低分类完成率，这正是 plan/04 §6 的
    第三道门槛。
    """
    scored = tuple(records)
    illegal_gold = sorted({record.gold_label for record in scored if record.gold_label not in LABELS})
    if illegal_gold:
        raise ValueError(f"人工标签不在三分类合同内：{illegal_gold}")
    scored_with_prediction = tuple(
        record for record in scored if _is_legal_label(record.agent_label)
    )

    confusion: dict[str, dict[str, int]] = {
        row: {column: 0 for column in LABELS} for row in LABELS
    }
    for record in scored_with_prediction:
        # 非法预测不进混淆矩阵（它们由 missing 分支报告），因此这里的列一定合法。
        confusion[record.gold_label][record.agent_label] += 1  # type: ignore[index]

    per_class: dict[str, ClassMetrics] = {}
    for label in LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[row][label] for row in LABELS if row != label)
        fn = sum(confusion[label][column] for column in LABELS if column != label)
        support = tp + fn
        precision = MetricValue(tp, tp + fp)
        recall = MetricValue(tp, support)
        # F1 用**等价的整数计数**表示（2·TP / (2·TP + FP + FN)），不走浮点：两个
        # 前提任一不成立时该类的 F1 就没有定义（召回率分母为 0 时尤其如此），
        # 记为不可计算而不是 0——0 会被误读成「算了，结果很差」。
        if precision.computable and recall.computable:
            f1 = MetricValue(2 * tp, 2 * tp + fp + fn)
        else:
            f1 = MetricValue(0, 0)
        per_class[label] = ClassMetrics(
            label=label,
            tp=tp,
            fp=fp,
            fn=fn,
            support=support,
            precision=precision,
            recall=recall,
            f1=f1,
        )

    # Macro-F1 是**三个** F1 的算术平均。任一类没有定义（该类没有人工样本）时
    # 整体记不可计算：plan/04 §6 明确「任一人工类别缺失…不宣称三分类验收完成」，
    # 把缺失类当 0 分算出来只会得到一个看起来能比较的数字。
    f1_values = [per_class[label].f1 for label in LABELS]
    if all(value.computable for value in f1_values):
        # 先通分相加再除以 3：F1 都是整数计数之比，保持精确比逐项转浮点更稳。
        numerator = 0
        denominator = 1
        for value in f1_values:
            numerator = numerator * value.denominator + value.numerator * denominator
            denominator *= value.denominator
        macro = MetricValue(numerator, denominator * len(LABELS)).reduced()
    else:
        macro = MetricValue(0, 0)

    correct = Label.CORRECT.value
    agreement_hits = sum(
        1 for record in scored_with_prediction if record.agent_label == record.gold_label
    )
    wrong_as_correct = MetricValue(
        sum(
            1
            for record in scored_with_prediction
            if record.gold_label != correct and record.agent_label == correct
        ),
        sum(1 for record in scored_with_prediction if record.gold_label != correct),
    )
    reviewed = sum(1 for record in scored_with_prediction if record.review_required)
    missing = tuple(record.record_id for record in scored if not _is_legal_label(record.agent_label))
    failed = tuple(
        record.record_id
        for record in scored
        if not _is_legal_label(record.agent_label)
        and (record.failure_stage is not None or record.status == "failed")
    )
    unfinished = tuple(record_id for record_id in missing if record_id not in set(failed))

    return EvaluationMetrics(
        scored=scored,
        excluded_ids=tuple(excluded_ids),
        valid_total=len(scored),
        confusion=confusion,
        per_class=per_class,
        macro_f1=macro,
        agreement=MetricValue(agreement_hits, len(scored_with_prediction)),
        wrong_as_correct=wrong_as_correct,
        completion=MetricValue(len(scored_with_prediction), len(scored)),
        review_ratio=MetricValue(reviewed, len(scored_with_prediction)),
        missing_ids=missing,
        failed_ids=failed,
        unfinished_ids=unfinished,
    )


def check_thresholds(metrics: EvaluationMetrics) -> tuple[ThresholdCheck, ...]:
    """plan/04 §6 的达标判定；不可计算的项 ``passed=None``（**不记通过**）。

    「每类至少 5 条」是**保留集**的门槛：校准时每类只有 1 条也属正常，因此该项
    只在保留集上才可能为真——调用方据 split 决定是否采信，报告里照实显示。
    """
    macro = metrics.macro_f1.value
    wrong = metrics.wrong_as_correct.value
    completion = metrics.completion.value
    thin_classes = [
        label
        for label, cell in metrics.per_class.items()
        if cell.support < MIN_CLASS_SUPPORT
    ]

    return (
        ThresholdCheck(
            key="wrong_as_correct",
            name="非正确类误判正确率",
            requirement=f"≤ {MAX_WRONG_AS_CORRECT:.0%}",
            actual=metrics.wrong_as_correct.display(),
            passed=None if wrong is None else wrong <= MAX_WRONG_AS_CORRECT,
        ),
        ThresholdCheck(
            key="macro_f1",
            name="Macro-F1",
            requirement=f"≥ {MIN_MACRO_F1:.2f}",
            actual=metrics.macro_f1.display(),
            passed=None if macro is None else macro >= MIN_MACRO_F1,
        ),
        ThresholdCheck(
            key="completion",
            name="分类完成率",
            requirement="= 100%",
            actual=metrics.completion.display(),
            passed=None if completion is None else completion >= 1.0,
        ),
        ThresholdCheck(
            key="class_support",
            name="每类样本数",
            requirement=f"每类 ≥ {MIN_CLASS_SUPPORT}",
            actual="全部达标"
            if not thin_classes
            else "、".join(
                f"{label} {metrics.per_class[label].support} 条" for label in thin_classes
            ),
            passed=not thin_classes,
        ),
    )


__all__ = [
    "DISPLAY_DIGITS",
    "MAX_WRONG_AS_CORRECT",
    "MIN_CLASS_SUPPORT",
    "MIN_MACRO_F1",
    "ClassMetrics",
    "EvaluationMetrics",
    "MetricValue",
    "ScoredRecord",
    "ThresholdCheck",
    "check_thresholds",
    "compute_metrics",
]
