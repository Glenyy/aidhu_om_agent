"""S07-02：固定分层划分与子集生成。

划分合同见 [plan/04 §2](../../../plan/04-测试与质量评估.md)：

- 随机种子固定为 ``20261005``，校准集约 20 条，其余为保留集；
- 按人工核定的**三类比例**分配，取最大余数法；
- 每类至少 1 条校准、至少 5 条保留；凑不齐时报错并提示补数据，**不缩分母、不改门槛**；
- 早期在真实模式下调试过的编号（见 `FORCED_CALIBRATION_IDS`）**强制入校准集**——
  它们已经参与过规则调整，放进保留集会污染正式验收（S07 阶段文档 §0.3 第 2 条）。

两个产物：

- ``split-v1.json``：种子、gold 版本与 sha256、计数、逐条归属、强制编号、生成时间。
  **它是防保留集泄漏的技术闸门**——`evaluate` 会核对「批次编号集合」与清单中指定
  split 的编号集合逐一同构。
- ``calibration-v1.xlsx``／``holdout-v1.xlsx``：只含 13 列输入原件的子集，直接上传
  即可建批次，避免手工筛错。

划分本身是**纯函数**（`plan_split`）：同样的 gold + 同样的种子 → 同样的归属；时间戳
只在写清单时加（`build_manifest`），因此可复现性测试不必和时钟较劲。
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook

from ..schemas.judgement import LABELS
from ..schemas.qa import INPUT_COLUMNS
from .gold import GOLD_SHEET, GoldEntry, GoldSet

#: 清单结构版本；字段增删时递增。
SPLIT_MANIFEST_VERSION = "1.0"

#: 固定随机种子（plan/04 §2）。
SPLIT_SEED = 20261005

#: 校准集目标条数；实际取 ``min(目标, 总数 - 保留集下限)``。
CALIBRATION_TARGET = 20

CALIBRATION = "calibration"
HOLDOUT = "holdout"
SPLITS: tuple[str, ...] = (CALIBRATION, HOLDOUT)

#: 每类的校准/保留下限（plan/04 §2）。
MIN_PER_CLASS_CALIBRATION = 1
MIN_PER_CLASS_HOLDOUT = 5

#: 早期在真实模式下调试过的编号，**强制入校准集**（S07 阶段文档 §0.3 第 2 条）。
#: S03 期间用记录 `1`、`4` 调试过提示词与重试；后续人工指定的调试编号也追加到这里。
FORCED_CALIBRATION_IDS: tuple[str, ...] = ("1", "4")

#: 产物文件名的版本后缀。
SPLIT_TAG = "v1"


def utc_now() -> str:
    """清单时间戳格式与 `storage.database.utc_now` 一致（UTC、秒级、含偏移）。

    本模块不依赖存储层：划分是纯计算，只借了同一个时间格式，便于两边对照。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SplitError(Exception):
    """划分无法按合同完成；``code`` 用于 CLI 与界面区分处理方式。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# --------------------------------------------------------------------------
# 结果类型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitPlan:
    """一次划分结果；``assignments`` 的**插入顺序即 gold 顺序**。"""

    seed: int
    calibration_target: int
    assignments: Mapping[str, str]
    label_counts: Mapping[str, Mapping[str, int]]
    forced_calibration: tuple[str, ...]

    def ids_for(self, split: str) -> tuple[str, ...]:
        """某个 split 的编号，按 gold 原顺序。未知 split 名报错。"""
        if split not in SPLITS:
            raise SplitError(
                "SPLIT_UNKNOWN", f"未知的 split 名 {split!r}；只接受 {'／'.join(SPLITS)}"
            )
        return tuple(
            record_id
            for record_id, assigned in self.assignments.items()
            if assigned == split
        )

    @property
    def calibration(self) -> tuple[str, ...]:
        return self.ids_for(CALIBRATION)

    @property
    def holdout(self) -> tuple[str, ...]:
        return self.ids_for(HOLDOUT)

    @property
    def total(self) -> int:
        return len(self.assignments)


@dataclass(frozen=True)
class SplitManifest:
    """``split-v1.json`` 的内容；`evaluate` 只读它，不重新划分。"""

    manifest_version: str
    seed: int
    calibration_target: int
    gold_filename: str
    gold_sha256: str
    gold_data_version: str
    gold_contract_version: str
    counts: Mapping[str, object]
    forced_calibration: tuple[str, ...]
    assignments: Mapping[str, str]
    generated_at: str

    @property
    def gold_sha256_prefix(self) -> str:
        return self.gold_sha256[:12]

    def ids_for(self, split: str) -> tuple[str, ...]:
        if split not in SPLITS:
            raise SplitError(
                "SPLIT_UNKNOWN", f"未知的 split 名 {split!r}；只接受 {'／'.join(SPLITS)}"
            )
        return tuple(
            record_id
            for record_id, assigned in self.assignments.items()
            if assigned == split
        )


# --------------------------------------------------------------------------
# 分配算法
# --------------------------------------------------------------------------


def calibration_size(total: int) -> int:
    """校准集条数：目标 20，但**保留每类 5 条**是硬底线，先扣掉再取小。"""
    return min(CALIBRATION_TARGET, total - MIN_PER_CLASS_HOLDOUT * len(LABELS))


def _largest_remainder(counts: Mapping[str, int], total: int) -> dict[str, int]:
    """最大余数法：先取整，再按余数从大到小补足差额。

    余数相同的类别按 `LABELS` 的固定顺序补——顺序稳定才能谈「同种子同划分」。
    """
    population = sum(counts.values())
    exact = {label: total * counts[label] / population for label in counts}
    quotas = {label: int(exact[label]) for label in counts}
    order = sorted(counts, key=lambda label: (-(exact[label] - quotas[label]), LABELS.index(label)))
    deficit = total - sum(quotas.values())
    for index in range(deficit):
        quotas[order[index % len(order)]] += 1
    return quotas


def _balance(quotas: dict[str, int], counts: Mapping[str, int], total: int) -> dict[str, int]:
    """把配额压回「每类 ≥1 且 ≤ 该类总数 − 保留下限」，再补齐到 ``total``。

    夹取会改变总和（例如某类被压到下限），因此夹取之后必须重新分配差额；分配时
    按剩余空间从大到小挑，平手按 `LABELS` 顺序——同样是为了可复现。
    """
    for label in LABELS:
        low = MIN_PER_CLASS_CALIBRATION
        high = counts[label] - MIN_PER_CLASS_HOLDOUT
        quotas[label] = max(low, min(quotas[label], high))

    while sum(quotas.values()) < total:
        candidates = [
            label
            for label in LABELS
            if quotas[label] < counts[label] - MIN_PER_CLASS_HOLDOUT
        ]
        if not candidates:  # pragma: no cover - 调用前已确保总量放得下
            raise SplitError("SPLIT_INFEASIBLE", "校准集配额无法补齐：每类保留下限已到顶")
        best = max(
            candidates,
            key=lambda label: (
                counts[label] - MIN_PER_CLASS_HOLDOUT - quotas[label],
                -LABELS.index(label),
            ),
        )
        quotas[best] += 1

    while sum(quotas.values()) > total:  # pragma: no cover - 夹取只会减少总量
        candidates = [label for label in LABELS if quotas[label] > MIN_PER_CLASS_CALIBRATION]
        if not candidates:
            raise SplitError("SPLIT_INFEASIBLE", "校准集配额无法收缩：每类下限已到顶")
        worst = min(
            candidates,
            key=lambda label: (quotas[label] - MIN_PER_CLASS_CALIBRATION, LABELS.index(label)),
        )
        quotas[worst] -= 1

    return quotas


def plan_split(
    gold: GoldSet,
    *,
    seed: int = SPLIT_SEED,
    forced_calibration: Sequence[str] = FORCED_CALIBRATION_IDS,
) -> SplitPlan:
    """按合同划分未排除记录；不满足数量/类别要求时抛 `SplitError`。

    **报错而不是缩分母**：凑不齐时正确的动作是补数据，不是把门槛降到凑得出数
    （plan/04 §2）。``forced_calibration`` 里不存在于核定文件的编号会被忽略
    （例如该条被人工排除），不报错。
    """
    included = gold.included
    if not included:
        raise SplitError("SPLIT_NO_RECORDS", "核定文件里没有被排除之外的可划分记录")

    by_label: dict[str, list[str]] = {label: [] for label in LABELS}
    for entry in included:
        if entry.label is None:  # pragma: no cover - gold 校验已拦住
            raise SplitError("SPLIT_UNLABELLED", f"记录 {entry.record_id} 没有人工标签")
        by_label[entry.label].append(entry.record_id)

    counts = {label: len(ids) for label, ids in by_label.items()}
    shortfall = {
        label: count
        for label, count in counts.items()
        if count < MIN_PER_CLASS_CALIBRATION + MIN_PER_CLASS_HOLDOUT
    }
    if shortfall:
        detail = "、".join(
            f"{label} {count} 条（至少需 {MIN_PER_CLASS_CALIBRATION + MIN_PER_CLASS_HOLDOUT} 条）"
            for label, count in shortfall.items()
        )
        raise SplitError(
            "SPLIT_INSUFFICIENT_CLASS",
            f"无法同时满足「每类校准 ≥{MIN_PER_CLASS_CALIBRATION}、保留 "
            f"≥{MIN_PER_CLASS_HOLDOUT}」：{detail}。请先补充该类的核定样本，"
            "本工具不会缩小分母或降低门槛。",
        )

    total = len(included)
    target = calibration_size(total)
    if target < MIN_PER_CLASS_CALIBRATION * len(LABELS):  # pragma: no cover - 上面已拦住
        raise SplitError("SPLIT_INFEASIBLE", "校准集目标条数不足以覆盖三类")

    quotas = _balance(_largest_remainder(counts, target), counts, target)

    # 强制入校准集的编号先占位；它们已参与过规则调整，绝不能落进保留集。
    forced = tuple(rid for rid in forced_calibration if rid in gold.included_ids)
    forced_by_label: dict[str, list[str]] = {label: [] for label in LABELS}
    for record_id in forced:
        label = next(entry.label for entry in included if entry.record_id == record_id)
        forced_by_label[label].append(record_id)
    for label, ids in forced_by_label.items():
        if len(ids) > quotas[label]:
            raise SplitError(
                "SPLIT_FORCED_EXCEEDS_QUOTA",
                f"强制入校准集的编号在「{label}」里有 {len(ids)} 条，超过该类校准配额 "
                f"{quotas[label]} 条；请调高校准集目标或补足该类样本",
            )

    rng = random.Random(seed)
    assignments: dict[str, str] = {}
    for label in LABELS:
        candidates = sorted(by_label[label])
        forced_here = forced_by_label[label]
        rest = [record_id for record_id in candidates if record_id not in set(forced_here)]
        rng.shuffle(rest)
        chosen = [*forced_here, *rest[: quotas[label] - len(forced_here)]]
        chosen_set = set(chosen)
        for record_id in candidates:
            assignments[record_id] = CALIBRATION if record_id in chosen_set else HOLDOUT

    # 归属按 gold 原顺序写出（dict 保序）：子集 xlsx 的行序与清单一致。
    ordered = {record_id: assignments[record_id] for record_id in gold.included_ids}

    label_counts: dict[str, dict[str, int]] = {}
    for split in SPLITS:
        per_label = {label: 0 for label in LABELS}
        for entry in included:
            if ordered[entry.record_id] == split:
                per_label[entry.label] += 1  # type: ignore[index]
        label_counts[split] = per_label

    return SplitPlan(
        seed=seed,
        calibration_target=target,
        assignments=ordered,
        label_counts=label_counts,
        forced_calibration=forced,
    )


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------


def manifest_payload(
    plan: SplitPlan, gold: GoldSet, *, generated_at: str
) -> dict[str, object]:
    """清单字典；``generated_at`` 由调用方给出，便于复现性测试固定时间戳。"""
    return {
        "manifest_version": SPLIT_MANIFEST_VERSION,
        "seed": plan.seed,
        "calibration_target": plan.calibration_target,
        "gold": {
            "filename": gold.path.name,
            "sha256": gold.sha256,
            "sha256_prefix": gold.sha256_prefix,
            "data_version": gold.metadata.data_version,
            "contract_version": gold.contract_version,
            "annotated_by": gold.metadata.annotated_by,
            "annotated_on": gold.metadata.annotated_on,
            "source_sha256_prefix": gold.metadata.source_sha256_prefix,
            "sheets": gold.sheet_name,
        },
        "counts": {
            "gold_total": len(gold.entries),
            "included": len(gold.included),
            "excluded": len(gold.excluded),
            "calibration": len(plan.calibration),
            "holdout": len(plan.holdout),
            "calibration_by_label": dict(plan.label_counts[CALIBRATION]),
            "holdout_by_label": dict(plan.label_counts[HOLDOUT]),
            "excluded_ids": [entry.record_id for entry in gold.excluded],
            "excluded_reasons": {
                entry.record_id: entry.exclusion_reason for entry in gold.excluded
            },
        },
        "forced_calibration": list(plan.forced_calibration),
        "assignments": dict(plan.assignments),
        "generated_at": generated_at,
    }


def write_manifest(path: Path, payload: Mapping[str, object]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return target


def read_manifest(path: str | Path) -> SplitManifest:
    """读回清单；字段缺失或类型不符即报错（清单是评估闸门，不能被含糊地读）。"""
    source = Path(path)
    if not source.is_file():
        raise SplitError("SPLIT_MANIFEST_MISSING", f"找不到划分清单：{source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SplitError("SPLIT_MANIFEST_INVALID", f"划分清单不是合法 JSON：{exc}") from exc

    try:
        gold = payload["gold"]
        counts = payload["counts"]
        assignments = payload["assignments"]
        manifest = SplitManifest(
            manifest_version=str(payload["manifest_version"]),
            seed=int(payload["seed"]),
            calibration_target=int(payload["calibration_target"]),
            gold_filename=str(gold["filename"]),
            gold_sha256=str(gold["sha256"]),
            gold_data_version=str(gold["data_version"]),
            gold_contract_version=str(gold["contract_version"]),
            counts=counts,
            forced_calibration=tuple(payload.get("forced_calibration", ())),
            assignments={str(key): str(value) for key, value in assignments.items()},
            generated_at=str(payload.get("generated_at", "")),
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SplitError(
            "SPLIT_MANIFEST_INVALID", f"划分清单缺少必需字段或类型不符：{exc!r}"
        ) from exc

    unknown = sorted(set(manifest.assignments.values()) - set(SPLITS))
    if unknown:
        raise SplitError("SPLIT_MANIFEST_INVALID", f"清单里有未知 split 值：{unknown}")
    return manifest


# --------------------------------------------------------------------------
# 子集工作簿
# --------------------------------------------------------------------------


def write_subset_workbook(path: str | Path, entries: Iterable[GoldEntry]) -> Path:
    """写出只含 13 列输入原件的子集工作簿，供直接上传建批次。

    核定列**不带进子集**：批次输入侧只有 13 列（[schemas.qa.INPUT_COLUMNS]），
    把人工标签一起传上去既无用处，也多一条泄漏路径。
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = GOLD_SHEET
    sheet.append(list(INPUT_COLUMNS))
    for entry in entries:
        record = entry.record
        sheet.append(
            [
                record.record_id,
                record.q,
                record.a,
                *[record.refs[field] for field in INPUT_COLUMNS[3:]],
            ]
        )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    return target


def _entries_for(gold: GoldSet, ids: Sequence[str]) -> tuple[GoldEntry, ...]:
    by_id = {entry.record_id: entry for entry in gold.entries}
    return tuple(by_id[record_id] for record_id in ids)


def generate_split(
    gold: GoldSet,
    directory: str | Path,
    *,
    seed: int = SPLIT_SEED,
    forced_calibration: Sequence[str] = FORCED_CALIBRATION_IDS,
    tag: str = SPLIT_TAG,
    overwrite: bool = False,
) -> tuple[Path, Path, Path]:
    """生成清单与两个子集工作簿，返回 ``(清单, 校准, 保留)`` 三个路径。

    **默认不覆盖已有产物**：划分一旦用于调试就冻结了，静默重写会让「保留集是否
    被用过」变得无法回答。要重新划分必须显式 ``overwrite=True``，并由调用方在
    报告里说明原因。
    """
    folder = Path(directory)
    manifest_path = folder / f"split-{tag}.json"
    calibration_path = folder / f"{CALIBRATION}-{tag}.xlsx"
    holdout_path = folder / f"{HOLDOUT}-{tag}.xlsx"

    if not overwrite:
        existing = [path for path in (manifest_path, calibration_path, holdout_path) if path.exists()]
        if existing:
            names = "、".join(path.name for path in existing)
            raise SplitError(
                "SPLIT_OUTPUT_EXISTS",
                f"产物已存在：{names}。划分一旦用于调试即冻结，重新划分必须先"
                "确认保留集未被使用，再用 overwrite 覆盖。",
            )

    plan = plan_split(gold, seed=seed, forced_calibration=forced_calibration)
    write_manifest(manifest_path, manifest_payload(plan, gold, generated_at=utc_now()))
    write_subset_workbook(calibration_path, _entries_for(gold, plan.calibration))
    write_subset_workbook(holdout_path, _entries_for(gold, plan.holdout))
    return manifest_path, calibration_path, holdout_path


__all__ = [
    "CALIBRATION",
    "CALIBRATION_TARGET",
    "FORCED_CALIBRATION_IDS",
    "HOLDOUT",
    "MIN_PER_CLASS_CALIBRATION",
    "MIN_PER_CLASS_HOLDOUT",
    "SPLITS",
    "SPLIT_MANIFEST_VERSION",
    "SPLIT_SEED",
    "SPLIT_TAG",
    "SplitError",
    "SplitManifest",
    "SplitPlan",
    "calibration_size",
    "generate_split",
    "manifest_payload",
    "plan_split",
    "read_manifest",
    "utc_now",
    "write_manifest",
    "write_subset_workbook",
]
