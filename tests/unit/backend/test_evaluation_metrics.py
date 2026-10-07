"""S07-03：混淆矩阵与分类指标（plan/04 E-17：用小矩阵手算核对）。

手算基准（行 = 人工真值，列 = agent 预测）：

               回答正确  未检索到正确资料  检索到正确资料但回答错误
回答正确            5            1                  0
未检索到正确资料      1            3                  0
检索到正确资料但回答错误 1            0                  1

另有 1 条人工标签为「回答正确」但**没有预测**的记录（技术失败），以及 3 条被
人工排除、不参与评分的记录。
"""

from __future__ import annotations

import pytest

from aidhu_om_agent.evaluation.metrics import (
    MAX_WRONG_AS_CORRECT,
    MIN_MACRO_F1,
    MetricValue,
    ScoredRecord,
    check_thresholds,
    compute_metrics,
)
from aidhu_om_agent.schemas.judgement import LABELS, Label

CORRECT, NO_REF, WRONG = LABELS


def record(
    record_id: str,
    gold: str,
    predicted: str | None,
    *,
    review: bool | None = False,
    status: str = "completed",
    failure_stage: str | None = None,
) -> ScoredRecord:
    return ScoredRecord(
        record_id=record_id,
        gold_label=gold,
        agent_label=predicted,
        review_required=review,
        status=status,
        failure_stage=failure_stage,
    )


def sample_records() -> list[ScoredRecord]:
    rows: list[ScoredRecord] = []
    index = 0

    def add(gold: str, predicted: str | None, **kwargs) -> None:
        nonlocal index
        index += 1
        rows.append(record(f"R-{index}", gold, predicted, **kwargs))

    for _ in range(5):
        add(CORRECT, CORRECT)
    add(CORRECT, NO_REF)
    add(NO_REF, CORRECT)
    for _ in range(3):
        add(NO_REF, NO_REF)
    add(WRONG, CORRECT)
    add(WRONG, WRONG)
    # 没有预测的一条：技术失败，仍计入有效记录（拉低分类完成率）
    add(CORRECT, None, status="failed", failure_stage="stage2")
    return rows


# --------------------------------------------------------------------------
# 手算核对
# --------------------------------------------------------------------------


def test_confusion_matrix_matches_hand_count() -> None:
    metrics = compute_metrics(sample_records())

    assert metrics.confusion[CORRECT] == {CORRECT: 5, NO_REF: 1, WRONG: 0}
    assert metrics.confusion[NO_REF] == {CORRECT: 1, NO_REF: 3, WRONG: 0}
    assert metrics.confusion[WRONG] == {CORRECT: 1, NO_REF: 0, WRONG: 1}


def test_per_class_metrics_match_hand_count() -> None:
    metrics = compute_metrics(sample_records())

    correct = metrics.per_class[CORRECT]
    assert (correct.tp, correct.fp, correct.fn, correct.support) == (5, 2, 1, 6)
    assert (correct.precision.numerator, correct.precision.denominator) == (5, 7)
    assert (correct.recall.numerator, correct.recall.denominator) == (5, 6)
    assert (correct.f1.numerator, correct.f1.denominator) == (10, 13)

    no_ref = metrics.per_class[NO_REF]
    assert (no_ref.tp, no_ref.fp, no_ref.fn, no_ref.support) == (3, 1, 1, 4)
    assert (no_ref.precision.numerator, no_ref.precision.denominator) == (3, 4)
    assert (no_ref.f1.numerator, no_ref.f1.denominator) == (6, 8)

    wrong = metrics.per_class[WRONG]
    assert (wrong.tp, wrong.fp, wrong.fn, wrong.support) == (1, 0, 1, 2)
    assert (wrong.precision.numerator, wrong.precision.denominator) == (1, 1)
    assert (wrong.recall.numerator, wrong.recall.denominator) == (1, 2)
    assert (wrong.f1.numerator, wrong.f1.denominator) == (2, 3)


def test_macro_f1_is_arithmetic_mean_of_three_f1() -> None:
    metrics = compute_metrics(sample_records())

    # (10/13 + 6/8 + 2/3) / 3 = 682/936 = 341/468（输出前约到最简）
    assert (metrics.macro_f1.numerator, metrics.macro_f1.denominator) == (341, 468)
    assert metrics.macro_f1.value == pytest.approx(0.728632, abs=1e-6)


def test_agreement_and_wrong_as_correct_match_hand_count() -> None:
    metrics = compute_metrics(sample_records())

    # 一致 = 5 + 3 + 1 = 9；参与评分 = 12
    assert (metrics.agreement.numerator, metrics.agreement.denominator) == (9, 12)
    # 人工非「回答正确」= 4 + 2 = 6；其中被判成「回答正确」= 1 + 1 = 2
    assert (metrics.wrong_as_correct.numerator, metrics.wrong_as_correct.denominator) == (2, 6)
    assert metrics.wrong_as_correct.value == pytest.approx(1 / 3)


def test_completion_counts_records_without_prediction() -> None:
    metrics = compute_metrics(sample_records())

    assert (metrics.completion.numerator, metrics.completion.denominator) == (12, 13)
    assert metrics.valid_total == 13
    assert metrics.scored_total == 13
    assert metrics.missing_ids == ("R-13",)
    assert metrics.failed_ids == ("R-13",)
    assert metrics.unfinished_ids == ()


def test_review_ratio_uses_scored_records_only() -> None:
    rows = [record(f"V-{index}", CORRECT, CORRECT, review=index <= 2) for index in range(1, 5)]
    metrics = compute_metrics(rows)

    assert (metrics.review_ratio.numerator, metrics.review_ratio.denominator) == (2, 4)


def test_review_required_records_still_count() -> None:
    """需复核的记录参与评分，不剔除（plan/04 §5）。"""
    rows = [
        record("V-1", CORRECT, CORRECT, review=True),
        record("V-2", CORRECT, NO_REF, review=True),
    ]
    metrics = compute_metrics(rows)

    assert metrics.agreement.numerator == 1
    assert metrics.agreement.denominator == 2
    assert metrics.confusion[CORRECT][NO_REF] == 1


# --------------------------------------------------------------------------
# 边界：零分母、未跑完、非法标签
# --------------------------------------------------------------------------


def test_missing_class_makes_macro_f1_not_computable() -> None:
    rows = [record("V-1", CORRECT, CORRECT), record("V-2", CORRECT, CORRECT)]
    metrics = compute_metrics(rows)

    assert metrics.per_class[WRONG].recall.computable is False
    assert metrics.per_class[WRONG].recall.value is None
    assert metrics.per_class[WRONG].f1.computable is False
    assert metrics.macro_f1.computable is False
    assert metrics.macro_f1.value is None
    assert "不可计算" in metrics.macro_f1.display()


def test_metric_with_zero_denominator_displays_as_not_computable() -> None:
    value = MetricValue(0, 0)

    assert value.computable is False
    assert value.value is None
    assert value.display() == "不可计算（分母为 0）"
    assert value.as_dict() == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
        "computable": False,
        "display": "不可计算（分母为 0）",
    }


def test_empty_evaluation_is_all_not_computable() -> None:
    metrics = compute_metrics([])

    assert metrics.completion.computable is False
    assert metrics.agreement.computable is False
    assert metrics.macro_f1.computable is False
    assert metrics.valid_total == 0


def test_illegal_agent_label_is_treated_as_missing() -> None:
    """标签不在三分类合同内的预测不算数，也不进混淆矩阵。"""
    rows = [record("V-1", CORRECT, CORRECT), record("V-2", CORRECT, "回答基本正确")]
    metrics = compute_metrics(rows)

    assert metrics.missing_ids == ("V-2",)
    assert metrics.completion.numerator == 1
    assert sum(sum(cells.values()) for cells in metrics.confusion.values()) == 1


def test_unfinished_records_are_not_marked_as_failures() -> None:
    rows = [
        record("V-1", CORRECT, CORRECT),
        record("V-2", CORRECT, None, status="pending"),
        record("V-3", CORRECT, None, status="failed", failure_stage="stage1"),
    ]
    metrics = compute_metrics(rows)

    assert metrics.failed_ids == ("V-3",)
    assert metrics.unfinished_ids == ("V-2",)
    assert metrics.missing_ids == ("V-2", "V-3")


def test_illegal_gold_label_is_rejected() -> None:
    with pytest.raises(ValueError):
        compute_metrics([record("V-1", "正确", CORRECT)])


# --------------------------------------------------------------------------
# 达标判定
# --------------------------------------------------------------------------


def test_thresholds_match_hand_checked_values() -> None:
    checks = {check.key: check for check in check_thresholds(compute_metrics(sample_records()))}

    assert checks["wrong_as_correct"].passed is False  # 2/6 = 33% > 5%
    assert checks["macro_f1"].passed is False  # 0.7286 < 0.80
    assert checks["completion"].passed is False  # 12/13
    assert checks["class_support"].passed is False  # 「检索到正确资料但回答错误」只有 2 条
    assert f"≤ {MAX_WRONG_AS_CORRECT:.0%}" in checks["wrong_as_correct"].requirement
    assert f"≥ {MIN_MACRO_F1:.2f}" in checks["macro_f1"].requirement


def test_passing_evaluation_meets_every_threshold() -> None:
    rows = [
        *[record(f"A-{index}", CORRECT, CORRECT) for index in range(20)],
        *[record(f"B-{index}", NO_REF, NO_REF) for index in range(10)],
        *[record(f"C-{index}", WRONG, WRONG) for index in range(6)],
    ]
    checks = {check.key: check for check in check_thresholds(compute_metrics(rows))}

    assert all(check.passed is True for check in checks.values())
    assert checks["wrong_as_correct"].actual == "0.0000（0/16）"
    assert checks["macro_f1"].actual == "1.0000（1/1）"
    assert checks["completion"].actual == "1.0000（36/36）"


def test_thresholds_are_not_passed_when_not_computable() -> None:
    checks = {check.key: check for check in check_thresholds(compute_metrics([]))}

    assert checks["wrong_as_correct"].passed is None
    assert checks["macro_f1"].passed is None
    assert checks["completion"].passed is None
    assert checks["class_support"].passed is False


def test_correct_label_is_the_reference_for_wrong_as_correct() -> None:
    """判错的样本不算「误判正确」，只有判成「回答正确」才算。"""
    rows = [
        record("V-1", Label.REF_OK_ANSWER_WRONG.value, Label.NO_CORRECT_REF.value),
        record("V-2", Label.REF_OK_ANSWER_WRONG.value, Label.CORRECT.value),
    ]
    metrics = compute_metrics(rows)

    assert (metrics.wrong_as_correct.numerator, metrics.wrong_as_correct.denominator) == (1, 2)
