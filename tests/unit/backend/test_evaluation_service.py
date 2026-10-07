"""S07-03：评估编排的三道闸门、落库、报告与只读视图。

用一条**合成的完整链路**核对（链路本身在 `fixtures/evaluation_scenario.py`）：
核定文件 → 划分清单 → 子集工作簿 → 批次 → 写入原预测 → `evaluate`。

三条底线在这里被反复验证：

1. **编号集合不一致就不算**（`ID_SET_MISMATCH`）：这是防保留集泄漏的主要闸门，
   拿保留集的批次去评校准集必须被挡住，并在消息里点出「多出的编号属于另一侧」。
2. **评分只用 agent 原预测**：技术失败与未完成**单独报告**、不进混淆矩阵；
   需复核记录照样参与评分。
3. **零模型调用**：评估前后 `jobs`、`call_attempts`、`artifacts` 的行数不变。

合成划分的手算值（33 条 = 15／10／8、种子固定）：校准 **18** 条 = 10／5／3，
保留 **15** 条 = 5／5／5。校准集「每类 ≥5」必然不满足（正确资料但回答错误只有
3 条），保留集恰好满足——这正是「每类 ≥5 只在保留集上说话」的口径。

全程合成数据、模拟预测，**零真实模型调用**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.evaluation_scenario import (
    CALIBRATION,
    CALIBRATION_PER_CLASS,
    CALIBRATION_SIZE,
    CORRECT,
    HOLDOUT,
    HOLDOUT_PER_CLASS,
    NO_REF,
    TOTAL,
    WRONG,
    Scenario,
    count_rows,
    fail_nth,
    flip_nth,
    reports_of,
    seed_scenario,
    subdir,
)
from fixtures.gold_samples import METADATA, gold_row, write_synthetic_gold
from test_cli_s05 import write_config

import aidhu_om_agent.cli as cli
from aidhu_om_agent.services.evaluation import (
    EvaluationError,
    evaluation_detail,
    list_evaluation_records,
    list_run_evaluations,
)

# --------------------------------------------------------------------------
# 闸门
# --------------------------------------------------------------------------


def test_unknown_run_is_rejected(tmp_path: Path) -> None:
    scenario = seed_scenario(tmp_path)

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate(run_id="0" * 32)

    assert excinfo.value.code == "NOT_FOUND"
    assert excinfo.value.http_status == 404


def test_unknown_split_is_rejected_before_reading_files(tmp_path: Path) -> None:
    """split 名非法时**先**报参数错：不该去读根本不存在的文件。"""
    scenario = seed_scenario(tmp_path)

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate(
            split="test",
            gold_path=tmp_path / "不存在的.xlsx",
            manifest_path=tmp_path / "不存在的.json",
        )

    assert excinfo.value.code == "SPLIT_UNKNOWN"
    assert excinfo.value.http_status == 422


def test_missing_gold_and_manifest_are_reported(tmp_path: Path) -> None:
    scenario = seed_scenario(tmp_path)

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate(gold_path=tmp_path / "没有这个文件.xlsx")
    assert excinfo.value.code == "GOLD_UNREADABLE"

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate(manifest_path=tmp_path / "没有这个清单.json")
    assert excinfo.value.code == "SPLIT_MANIFEST_MISSING"


def test_unfinished_run_is_rejected(tmp_path: Path) -> None:
    """还在跑的批次不评：算出来的是立刻过期的报告。"""
    scenario = seed_scenario(tmp_path, run_status="running")

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate()

    assert excinfo.value.code == "RUN_NOT_FINISHED"
    assert excinfo.value.details["status"] == "running"


def test_invalid_gold_reports_every_problem(tmp_path: Path) -> None:
    """核定文件本身不合法时，报出逐条问题，而不是一句「读不了」。"""
    scenario = seed_scenario(tmp_path)
    broken = write_synthetic_gold(
        tmp_path / "broken.xlsx",
        [
            gold_row("1", judgement=CORRECT, reason="理由一"),
            gold_row("2", judgement="回答基本正确", reason="标签不在合同内"),
        ],
    )

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate(gold_path=broken)

    assert excinfo.value.code == "GOLD_INVALID"
    problems = excinfo.value.details["problems"]
    assert problems and all(item["code"] and item["message"] for item in problems)


def test_manifest_from_another_gold_is_rejected(tmp_path: Path) -> None:
    """清单里的 gold 摘要与本次文件不同 → 不能假设「编号该在哪一侧」。"""
    scenario = seed_scenario(tmp_path)
    other = write_synthetic_gold(
        tmp_path / "另一份.xlsx",
        [gold_row(str(i), judgement=CORRECT, reason="另一份答案") for i in range(1, TOTAL + 1)],
        metadata={**METADATA, "数据版本": "synthetic-v2"},
    )

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate(gold_path=other)

    assert excinfo.value.code == "GOLD_MISMATCH"
    details = excinfo.value.details
    assert details["manifest_gold_sha256"] != details["gold_sha256"]


def test_cross_split_batch_is_rejected(tmp_path: Path) -> None:
    """**防泄漏主闸门**：用保留集的子集建批次、却按校准集评估 → 拒绝并点名。"""
    scenario = seed_scenario(tmp_path, split=CALIBRATION, batch_split=HOLDOUT)

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate()

    assert excinfo.value.code == "ID_SET_MISMATCH"
    details = excinfo.value.details
    assert details["other_split"], "应指出多出的编号属于另一侧"
    assert "另一侧" in excinfo.value.message
    # 批次里是保留集的 15 条（全部被认出属于另一侧），缺的正是校准集的 18 条。
    assert len(details["other_split"]) == sum(HOLDOUT_PER_CLASS.values())
    assert details["other_split"] == sorted(details["extra"]), "多出的都该被认出属于另一侧"
    assert len(details["missing"]) == CALIBRATION_SIZE
    assert details["missing"] == sorted(scenario.ids)


def test_batch_missing_ids_is_rejected(tmp_path: Path) -> None:
    """批次少了几条（同侧）→ 拒绝，报出缺失编号，且不算成跨侧泄漏。"""
    scenario = seed_scenario(tmp_path, split=CALIBRATION, drop_ids=2)

    with pytest.raises(EvaluationError) as excinfo:
        scenario.evaluate()

    assert excinfo.value.code == "ID_SET_MISMATCH"
    details = excinfo.value.details
    assert details["other_split"] == []
    assert details["extra"] == []
    assert sorted(details["missing"]) == sorted(scenario.ids[-2:])


# --------------------------------------------------------------------------
# 成功路径：指标、落库、报告
# --------------------------------------------------------------------------


def test_evaluation_persists_metrics_and_reports(tmp_path: Path) -> None:
    scenario = seed_scenario(tmp_path)

    outcome = scenario.evaluate()

    assert outcome.split == CALIBRATION
    assert outcome.metrics.valid_total == CALIBRATION_SIZE
    # 全部判对：Macro-F1、一致率、完成率都是 1；非正确类一条都没被误判成正确。
    assert outcome.metrics.macro_f1.display() == "1.0000（1/1）"
    assert outcome.metrics.agreement.display() == "1.0000（18/18）"
    assert outcome.metrics.completion.display() == "1.0000（18/18）"
    assert outcome.metrics.wrong_as_correct.display() == "0.0000（0/8）"
    assert outcome.metrics.missing_ids == ()
    for label, count in CALIBRATION_PER_CLASS.items():
        assert outcome.metrics.per_class[label].support == count

    checks = {check.key: check for check in outcome.checks}
    assert checks["macro_f1"].passed is True
    assert checks["completion"].passed is True
    assert checks["wrong_as_correct"].passed is True
    # 「每类 ≥5」是保留集门槛：校准集里 WRONG 只有 3 条，这里必然为假。
    assert checks["class_support"].passed is False
    assert outcome.passed is False

    assert outcome.report_json_path.is_file()
    assert outcome.report_text_path.is_file()
    assert outcome.report_json_path.parent == reports_of(scenario.config)

    payload = json.loads(outcome.report_json_path.read_text(encoding="utf-8"))
    assert payload["evaluation_id"] == outcome.evaluation_id
    assert payload["split"] == CALIBRATION
    assert len(payload["records"]) == CALIBRATION_SIZE
    assert payload["freeze"]["gold"]["sha256"] == scenario.gold.sha256
    assert payload["freeze"]["manifest"]["sha256"]
    assert payload["split_info"]["seed"] == 20261005
    assert payload["split_info"]["forced_calibration"] == ["1", "4"]
    assert len(payload["records"][0]) == 8

    assert count_rows(scenario.database, "evaluations") == 1
    assert count_rows(scenario.database, "evaluation_records") == CALIBRATION_SIZE


def test_confusion_matrix_follows_the_batch_predictions(tmp_path: Path) -> None:
    """按类别故意判错：矩阵与逐类指标跟着变（期望值手算）。"""
    scenario = seed_scenario(
        tmp_path, answers=flip_nth({CORRECT: (2, NO_REF), WRONG: (1, CORRECT)})
    )

    outcome = scenario.evaluate()

    confusion = outcome.metrics.confusion
    # 2 条「回答正确」被判成「未检索到正确资料」。
    assert confusion[CORRECT][CORRECT] == 8
    assert confusion[CORRECT][NO_REF] == 2
    assert confusion[CORRECT][WRONG] == 0
    # 1 条「检索到正确资料但回答错误」被判成「回答正确」：这就是「非正确类误判正确」。
    assert confusion[WRONG][CORRECT] == 1
    assert confusion[WRONG][WRONG] == 2
    assert confusion[NO_REF][NO_REF] == 5

    correct = outcome.metrics.per_class[CORRECT]
    assert (correct.tp, correct.fp, correct.fn) == (8, 1, 2)
    assert (correct.precision.numerator, correct.precision.denominator) == (8, 9)
    assert (correct.recall.numerator, correct.recall.denominator) == (8, 10)
    # 非正确类共 8 条（5 + 3），其中 1 条被判成「回答正确」。
    assert outcome.metrics.wrong_as_correct.display() == "0.1250（1/8）"
    # 一致 15 条（8 + 5 + 2）/ 18。
    assert outcome.metrics.agreement.display() == "0.8333（15/18）"


def test_technical_failure_is_reported_separately(tmp_path: Path) -> None:
    """技术失败**不给标签、不进混淆矩阵**，只拉低分类完成率。"""
    scenario = seed_scenario(tmp_path, answers=fail_nth(WRONG, 2))
    broken = scenario.by_class()[WRONG][1]

    outcome = scenario.evaluate()

    assert outcome.metrics.failed_ids == (broken,)
    assert outcome.metrics.unfinished_ids == ()
    assert outcome.metrics.missing_ids == (broken,)
    assert outcome.metrics.completion.display() == "0.9444（17/18）"
    assert sum(sum(cells.values()) for cells in outcome.metrics.confusion.values()) == 17
    checks = {check.key: check for check in outcome.checks}
    assert checks["completion"].passed is False

    detail = evaluation_detail(
        scenario.database, outcome.evaluation_id, config=scenario.config
    )
    assert detail["counts"] == {"valid": 18, "scored": 17, "excluded": 0, "missing": 1}
    rows = list_evaluation_records(
        scenario.database, outcome.evaluation_id, record_id=broken
    )
    assert rows["items"][0]["agent_label"] is None
    assert rows["items"][0]["agree"] is None
    assert rows["items"][0]["record_status"] == "failed"


def test_review_required_records_still_score(tmp_path: Path) -> None:
    """需复核记录**参与评分、不剔除**，只体现在复核比例上。"""
    probe = seed_scenario(subdir(tmp_path, "a"))
    outcome = seed_scenario(subdir(tmp_path, "b"), review_ids=probe.ids[:3]).evaluate()

    assert outcome.metrics.review_ratio.display() == "0.1667（3/18）"
    assert outcome.metrics.completion.denominator == CALIBRATION_SIZE
    assert outcome.metrics.agreement.denominator == CALIBRATION_SIZE


def test_evaluate_calls_no_model_and_queues_nothing(tmp_path: Path) -> None:
    """**零模型调用**：评估不改任务表、调用台账与产物表。"""
    scenario = seed_scenario(tmp_path)
    tables = ("jobs", "call_attempts", "artifacts", "exports")
    before = {table: count_rows(scenario.database, table) for table in tables}

    scenario.evaluate()
    scenario.evaluate()

    assert {table: count_rows(scenario.database, table) for table in tables} == before


def test_repeated_evaluations_are_kept_as_history(tmp_path: Path) -> None:
    """同一批次可以评多次，每次都留痕、互不覆盖。"""
    scenario = seed_scenario(tmp_path)

    first = scenario.evaluate()
    second = scenario.evaluate()

    assert first.evaluation_id != second.evaluation_id
    listed = list_run_evaluations(scenario.database, scenario.run_id)
    assert listed["total"] == 2
    assert {item["evaluation_id"] for item in listed["items"]} == {
        first.evaluation_id,
        second.evaluation_id,
    }
    assert count_rows(scenario.database, "evaluation_records") == 2 * CALIBRATION_SIZE


def test_holdout_split_scores_the_other_side(tmp_path: Path) -> None:
    """同一个编排也能评保留集：两侧走完全相同的代码路径。"""
    scenario = seed_scenario(tmp_path, split=HOLDOUT, batch_split=HOLDOUT)

    outcome = scenario.evaluate()

    assert outcome.split == HOLDOUT
    assert outcome.metrics.valid_total == sum(HOLDOUT_PER_CLASS.values())
    for label, count in HOLDOUT_PER_CLASS.items():
        assert outcome.metrics.per_class[label].support == count
    checks = {check.key: check for check in outcome.checks}
    assert checks["class_support"].passed is True  # 保留集 5／5／5
    assert outcome.passed is True
    assert outcome.report_text_path.name.endswith("holdout.md")


# --------------------------------------------------------------------------
# 只读视图
# --------------------------------------------------------------------------


def test_read_views_expose_the_frozen_numbers(tmp_path: Path) -> None:
    scenario = seed_scenario(tmp_path)
    outcome = scenario.evaluate()

    listed = list_run_evaluations(scenario.database, scenario.run_id)
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["evaluation_id"] == outcome.evaluation_id
    assert item["macro_f1"] == "1.0000（1/1）"
    assert item["scored_count"] == CALIBRATION_SIZE
    assert item["split"] == CALIBRATION

    detail = evaluation_detail(
        scenario.database, outcome.evaluation_id, config=scenario.config
    )
    assert detail["metrics"]["macro_f1"]["numerator"] == 1
    assert detail["thresholds"] and all("passed" in check for check in detail["thresholds"])
    assert detail["split_info"]["excluded_ids"] == []
    assert detail["freeze"]["stage1_prompt_version"] == "1.2"
    assert detail["freeze"]["model_config"]["stage1"]["model"] == "test-model"
    assert [item["kind"] for item in detail["report_files"]] == ["text", "json"]
    assert all(item["exists"] for item in detail["report_files"])
    assert detail["report_text"].startswith("# 评估报告")
    assert "## 混淆矩阵" in detail["report_text"]
    assert "## 冻结清单" in detail["report_text"]
    assert detail["run"]["status"] == "completed"


def test_record_view_filters_and_paginates(tmp_path: Path) -> None:
    scenario = seed_scenario(tmp_path, answers=flip_nth({CORRECT: (99, NO_REF)}))
    outcome = scenario.evaluate()

    disagree = list_evaluation_records(
        scenario.database, outcome.evaluation_id, agree=False
    )
    assert disagree["total"] == CALIBRATION_PER_CLASS[CORRECT]
    assert all(row["agree"] is False for row in disagree["items"])

    agree = list_evaluation_records(scenario.database, outcome.evaluation_id, agree=True)
    assert agree["total"] == CALIBRATION_SIZE - CALIBRATION_PER_CLASS[CORRECT]

    second = list_evaluation_records(
        scenario.database, outcome.evaluation_id, page=2, page_size=5
    )
    first = list_evaluation_records(
        scenario.database, outcome.evaluation_id, page=1, page_size=5
    )
    assert second["total"] == CALIBRATION_SIZE
    assert len(second["items"]) == 5
    assert not {row["record_id"] for row in second["items"]} & {
        row["record_id"] for row in first["items"]
    }


def test_views_reject_unknown_ids_and_bad_paging(tmp_path: Path) -> None:
    scenario = seed_scenario(tmp_path)

    for call in (
        lambda: list_run_evaluations(scenario.database, "0" * 32),
        lambda: evaluation_detail(scenario.database, "0" * 32, config=scenario.config),
        lambda: list_evaluation_records(scenario.database, "0" * 32),
    ):
        with pytest.raises(EvaluationError) as excinfo:
            call()
        assert excinfo.value.code == "NOT_FOUND"

    with pytest.raises(EvaluationError) as excinfo:
        list_run_evaluations(scenario.database, scenario.run_id, page_size=0)
    assert excinfo.value.code == "PARAM_VALIDATION"
    assert excinfo.value.http_status == 422

    with pytest.raises(EvaluationError) as excinfo:
        list_evaluation_records(scenario.database, "0" * 32, page_size=999)
    assert excinfo.value.code == "PARAM_VALIDATION"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def evaluate_argv(scenario: Scenario, *, split: str = CALIBRATION) -> list[str]:
    return [
        "evaluate",
        "--config",
        str(scenario.config.source_config),
        "--run-id",
        scenario.run_id,
        "--gold",
        str(scenario.gold.path),
        "--split-manifest",
        str(scenario.manifest_path),
        "--split",
        split,
    ]


def test_cli_evaluate_reports_the_same_numbers(tmp_path: Path, capsys) -> None:
    scenario = seed_scenario(tmp_path, config_path=write_config(tmp_path))

    code = cli.main(evaluate_argv(scenario))
    out = capsys.readouterr().out

    assert code == 0
    assert "评估完成：evaluation_id=" in out
    assert "Macro-F1 1.0000（1/1）" in out
    assert "分类完成率 1.0000（18/18）" in out
    assert "[达标] Macro-F1" in out
    assert "[未达标] 每类样本数" in out
    # 报告落在**配置指向的**目录，没写到开发机的真实数据目录。
    assert reports_of(scenario.config).is_dir()
    assert sorted(path.suffix for path in reports_of(scenario.config).iterdir()) == [
        ".json",
        ".md",
    ]
    assert count_rows(scenario.database, "evaluations") == 1


def test_cli_evaluate_returns_2_on_a_gate_failure(tmp_path: Path, capsys) -> None:
    scenario = seed_scenario(
        tmp_path, config_path=write_config(tmp_path), batch_split=HOLDOUT
    )

    code = cli.main(evaluate_argv(scenario))
    err = capsys.readouterr().err

    assert code == 2
    assert "[ID_SET_MISMATCH]" in err
    assert count_rows(scenario.database, "evaluations") == 0


def test_cli_evaluate_reports_config_errors_as_1(tmp_path: Path, capsys) -> None:
    """配置读不出来是环境问题（1），与「状态不适用」（2）分开。"""
    code = cli.main(
        [
            "evaluate",
            "--config",
            str(tmp_path / "没有这份配置.toml"),
            "--run-id",
            "r1",
            "--gold",
            "gold.xlsx",
            "--split-manifest",
            "split.json",
            "--split",
            CALIBRATION,
        ]
    )
    err = capsys.readouterr().err

    assert code == 1
    assert "配置错误" in err
