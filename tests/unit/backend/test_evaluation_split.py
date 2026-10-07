"""S07-02：固定分层划分、清单与子集生成。

重点核对三件事：**可复现**（同种子同划分）、**不交叉且覆盖**（校准与保留是全集
的一个划分）、**门槛不被悄悄放宽**（凑不齐就报错并提示补数据）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.evaluation_scenario import make_gold
from fixtures.gold_samples import METADATA, gold_row, write_synthetic_gold
from openpyxl import load_workbook

from aidhu_om_agent.evaluation.gold import GoldSet, read_gold
from aidhu_om_agent.evaluation.split import (
    CALIBRATION,
    CALIBRATION_TARGET,
    FORCED_CALIBRATION_IDS,
    HOLDOUT,
    MIN_PER_CLASS_HOLDOUT,
    SPLIT_SEED,
    SplitError,
    calibration_size,
    generate_split,
    manifest_payload,
    plan_split,
    read_manifest,
)
from aidhu_om_agent.schemas.judgement import LABELS
from aidhu_om_agent.schemas.qa import INPUT_COLUMNS

CORRECT, NO_REF, WRONG = LABELS


# --------------------------------------------------------------------------
# 份额计算
# --------------------------------------------------------------------------


def test_calibration_size_keeps_holdout_floor() -> None:
    assert calibration_size(100) == CALIBRATION_TARGET
    assert calibration_size(18) == 3  # 每类保底 5 条，只能留 3 个校准位
    assert calibration_size(24) == 9
    assert calibration_size(40) == 20


def test_plan_meets_every_floor(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    plan = plan_split(gold)

    assert plan.total == 100
    assert len(plan.calibration) == 20
    assert len(plan.holdout) == 80
    assert plan.calibration_target == 20
    # 三类比例 60/25/15 → 20 条校准按同比例得 12/5/3
    assert plan.label_counts[CALIBRATION] == {CORRECT: 12, NO_REF: 5, WRONG: 3}
    assert plan.label_counts[HOLDOUT] == {CORRECT: 48, NO_REF: 20, WRONG: 12}


def test_split_is_a_partition(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    plan = plan_split(gold)

    assert set(plan.calibration).isdisjoint(plan.holdout)
    assert set(plan.calibration) | set(plan.holdout) == set(gold.included_ids)
    assert plan.total == len(gold.included)


def test_largest_remainder_breaks_ties_by_remainder(tmp_path: Path) -> None:
    """12/6/6 共 24 条、校准 9 条：精确值为 4.5/2.25/2.25，最大余数先给「回答正确」。"""
    gold = make_gold(tmp_path, {CORRECT: 12, NO_REF: 6, WRONG: 6})
    plan = plan_split(gold)

    assert plan.calibration_target == 9
    assert plan.label_counts[CALIBRATION] == {CORRECT: 7, NO_REF: 1, WRONG: 1}
    assert plan.label_counts[HOLDOUT] == {CORRECT: 5, NO_REF: 5, WRONG: 5}


def test_reproducible_for_the_same_seed(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})

    first = plan_split(gold)
    second = plan_split(gold)

    assert first == second
    assert manifest_payload(first, gold, generated_at="2026-10-07T00:00:00+00:00") == (
        manifest_payload(second, gold, generated_at="2026-10-07T00:00:00+00:00")
    )


def test_different_seed_gives_a_different_split(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})

    assert plan_split(gold, seed=SPLIT_SEED).assignments != (
        plan_split(gold, seed=SPLIT_SEED + 1).assignments
    )


def test_forced_ids_land_in_calibration(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    plan = plan_split(gold)

    assert plan.forced_calibration == FORCED_CALIBRATION_IDS
    for record_id in FORCED_CALIBRATION_IDS:
        assert plan.assignments[record_id] == CALIBRATION
        assert record_id in plan.calibration
        assert record_id not in plan.holdout


def test_forced_ids_missing_from_gold_are_ignored(tmp_path: Path) -> None:
    """强制编号被人工排除时它不在可划分集合里，安静跳过而不是报错。"""
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    plan = plan_split(gold, forced_calibration=("1", "4", "999"))

    assert plan.forced_calibration == ("1", "4")


def test_forced_ids_beyond_quota_are_rejected(tmp_path: Path) -> None:
    """某一类的校准配额只有 2 个，要塞 3 个强制编号时必须报错，而不是挤掉别人。"""
    gold = make_gold(tmp_path, {CORRECT: 20, NO_REF: 7, WRONG: 7})

    # 编号 21—27 属「未检索到正确资料」类；该类保留下限 5 条 → 校准位只有 2 个。
    with pytest.raises(SplitError) as info:
        plan_split(gold, forced_calibration=("21", "22", "23"))

    assert info.value.code == "SPLIT_FORCED_EXCEEDS_QUOTA"


# --------------------------------------------------------------------------
# 数量不足：报错而不是缩分母
# --------------------------------------------------------------------------


def test_class_below_floor_is_rejected_with_supplement_hint(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 3})

    with pytest.raises(SplitError) as info:
        plan_split(gold)

    assert info.value.code == "SPLIT_INSUFFICIENT_CLASS"
    assert WRONG in info.value.message
    assert "先补充" in info.value.message


def test_every_split_respects_the_holdout_floor(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 12, NO_REF: 6, WRONG: 6})
    plan = plan_split(gold)

    for label in LABELS:
        assert plan.label_counts[HOLDOUT][label] >= MIN_PER_CLASS_HOLDOUT
        assert plan.label_counts[CALIBRATION][label] >= 1


def test_unknown_split_name_is_rejected(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    plan = plan_split(gold)

    with pytest.raises(SplitError) as info:
        plan.ids_for("validation")

    assert info.value.code == "SPLIT_UNKNOWN"


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------


def test_generate_split_writes_manifest_and_two_subsets(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    folder = tmp_path / "out"

    manifest_path, calibration_path, holdout_path = generate_split(gold, folder)

    assert manifest_path.name == "split-v1.json"
    assert calibration_path.name == "calibration-v1.xlsx"
    assert holdout_path.name == "holdout-v1.xlsx"

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["seed"] == SPLIT_SEED
    assert payload["gold"]["sha256"] == gold.sha256
    assert payload["gold"]["sha256_prefix"] == gold.sha256[:12]
    assert payload["counts"]["included"] == 100
    assert payload["counts"]["calibration"] == 20
    assert payload["counts"]["holdout"] == 80
    assert payload["counts"]["calibration_by_label"] == {CORRECT: 12, NO_REF: 5, WRONG: 3}
    assert len(payload["assignments"]) == 100
    assert payload["forced_calibration"] == list(FORCED_CALIBRATION_IDS)
    assert payload["generated_at"]


def test_subset_workbooks_carry_only_the_13_input_columns(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    _, calibration_path, holdout_path = generate_split(gold, tmp_path / "out")

    workbook = load_workbook(calibration_path, read_only=True)
    rows = list(workbook[workbook.sheetnames[0]].iter_rows(values_only=True))
    workbook.close()

    assert tuple(rows[0]) == INPUT_COLUMNS
    assert len(rows) - 1 == 20
    ids = [str(row[0]) for row in rows[1:]]
    assert ids == list(plan_split(gold).calibration)

    holdout = load_workbook(holdout_path, read_only=True)
    holdout_rows = list(holdout[holdout.sheetnames[0]].iter_rows(values_only=True))
    holdout.close()
    assert len(holdout_rows) - 1 == 80


def test_generate_split_refuses_to_overwrite(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    folder = tmp_path / "out"
    generate_split(gold, folder)

    with pytest.raises(SplitError) as info:
        generate_split(gold, folder)

    assert info.value.code == "SPLIT_OUTPUT_EXISTS"
    assert "split-v1.json" in info.value.message


def test_generate_split_overwrites_when_asked(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    folder = tmp_path / "out"
    generate_split(gold, folder)
    # 先毁掉一个产物：覆盖成功与否看它是否被重新写成可读的工作簿，而不是比字节
    # （xlsx 内含秒级时间戳，同一秒内两次生成的字节可能完全一样）。
    (folder / "calibration-v1.xlsx").write_bytes(b"not-a-workbook")

    generate_split(gold, folder, overwrite=True)

    workbook = load_workbook(folder / "calibration-v1.xlsx", read_only=True)
    rows = list(workbook[workbook.sheetnames[0]].iter_rows(values_only=True))
    workbook.close()
    assert len(rows) - 1 == 20


# --------------------------------------------------------------------------
# 清单读回
# --------------------------------------------------------------------------


def test_manifest_round_trip(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    manifest_path, _, _ = generate_split(gold, tmp_path / "out")

    manifest = read_manifest(manifest_path)

    assert manifest.seed == SPLIT_SEED
    assert manifest.calibration_target == 20
    assert manifest.gold_filename == "gold.xlsx"
    assert manifest.gold_sha256 == gold.sha256
    assert manifest.gold_sha256_prefix == gold.sha256[:12]
    assert manifest.gold_data_version == "synthetic-v1"
    assert len(manifest.ids_for(CALIBRATION)) == 20
    assert len(manifest.ids_for(HOLDOUT)) == 80
    assert set(manifest.ids_for(CALIBRATION)) | set(manifest.ids_for(HOLDOUT)) == set(
        gold.included_ids
    )


def test_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SplitError) as info:
        read_manifest(tmp_path / "没有这个清单.json")

    assert info.value.code == "SPLIT_MANIFEST_MISSING"


def test_manifest_with_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "split-v1.json"
    path.write_text("{不是 JSON", encoding="utf-8")

    with pytest.raises(SplitError) as info:
        read_manifest(path)

    assert info.value.code == "SPLIT_MANIFEST_INVALID"


def test_manifest_without_required_field(tmp_path: Path) -> None:
    path = tmp_path / "split-v1.json"
    path.write_text(json.dumps({"seed": 1}), encoding="utf-8")

    with pytest.raises(SplitError) as info:
        read_manifest(path)

    assert info.value.code == "SPLIT_MANIFEST_INVALID"


def test_manifest_with_unknown_split_value(tmp_path: Path) -> None:
    gold = make_gold(tmp_path, {CORRECT: 60, NO_REF: 25, WRONG: 15})
    manifest_path, _, _ = generate_split(gold, tmp_path / "out")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_id = next(iter(payload["assignments"]))
    payload["assignments"][first_id] = "validation"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SplitError) as info:
        read_manifest(manifest_path)

    assert info.value.code == "SPLIT_MANIFEST_INVALID"
