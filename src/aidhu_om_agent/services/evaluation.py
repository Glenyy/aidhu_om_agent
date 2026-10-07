"""评估编排：核对闸门 → 计算指标 → 落库 + 落盘报告（S07-03）。

`evaluate`（CLI）与评估展示页（只读）都走这里。**本模块零模型调用、不依赖 worker**：
它只读已经落库的原预测与人工核定文件，做算术，然后写一条评估记录。

三道闸门（S07 阶段文档 §0.3 第 3 条与 §2 的完成条件）：

1. **批次存在且已到终态**——还在跑的批次算出来的是一份会过期的报告；
2. **核定文件与划分清单对得上**——清单里的 gold sha256 必须等于本次核定文件的
   摘要，否则清单和答案不是同一份，「这批编号该在哪一侧」就无从谈起；
3. **编号集合同构**——批次里的编号集合必须与清单中指定 split 的编号集合逐一相同。
   这是**防保留集泄漏的主要技术闸门**：拿校准集的批次去评保留集，或者批次里混进
   了另一侧的编号，都在这里被挡住。报错时单独指出「多出的编号属于另一侧」。

计分口径固定为 `records.final_label`（agent 原预测）：需复核记录参与评分、不剔除；
人工修订不计作预测（人工修订回读属 S08 及以后，本阶段没有写入口，字段先按口岸留白）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..config import AppConfig
from ..evaluation.gold import (
    GoldError,
    GoldSet,
    GoldValidationError,
    compare_ids,
    read_gold,
)
from ..evaluation.metrics import (
    EvaluationMetrics,
    ScoredRecord,
    ThresholdCheck,
    check_thresholds,
    compute_metrics,
)
from ..evaluation.split import (
    CALIBRATION,
    HOLDOUT,
    SPLITS,
    SplitError,
    SplitManifest,
    read_manifest,
)
from ..excel.reader import file_digest
from ..repositories import evaluations as evaluations_repo
from ..repositories import records as records_repo
from ..repositories import runs as runs_repo
from ..schemas.judgement import LABELS
from ..storage import (
    Database,
    read_transaction,
    utc_now,
    write_transaction,
)
from ..version import package_version
from .batches import model_config_snapshot

#: 可以开始评估的批次状态。`queued`／`running` 的批次还会变，评估它得到的是一份
#: 立刻过期的报告；其余四个都是终态，其中 `partial_failed` 正是 §0.3 第 5 条
#: 「技术失败单独报告」要评的对象。
TERMINAL_RUN_STATUSES = frozenset({"completed", "partial_failed", "failed", "interrupted"})

#: 逐条对照的默认分页（与记录列表同口径）。
RECORD_PAGE_SIZE_DEFAULT = 50
RECORD_PAGE_SIZE_MAX = 200

#: 评估历史的分页。
EVALUATION_PAGE_SIZE_DEFAULT = 20
EVALUATION_PAGE_SIZE_MAX = 100


class EvaluationError(Exception):
    """评估被拒绝；``code`` 供 CLI 与界面区分处理方式（沿用 plan/08 错误码风格）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


@dataclass(frozen=True)
class EvaluationOutcome:
    """一次评估的结果；报告路径指向**已落盘**的两份文件。"""

    evaluation_id: str
    run_id: str
    split: str
    metrics: EvaluationMetrics
    checks: tuple[ThresholdCheck, ...]
    report_json_path: Path
    report_text_path: Path
    warnings: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """全部达标项都为真才算通过；不可计算（``None``）**不算通过**。"""
        return all(check.passed is True for check in self.checks)


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def evaluation_root(config: AppConfig) -> Path:
    """评估目录 ``data/evaluation/``（S07 阶段文档 §0.3 第 6 条）。

    与数据库同级推导，而不是新增配置键：`data/` 就是「这台机器的数据目录」，
    核定文件、划分清单与报告都放在它下面，用户只需要记住一个地方。
    """
    return config.paths.database.parent / "evaluation"


def reports_dir(config: AppConfig) -> Path:
    """报告目录 ``data/evaluation/reports/``。"""
    return evaluation_root(config) / "reports"


# --------------------------------------------------------------------------
# 闸门与计算
# --------------------------------------------------------------------------


def _load_gold(path: Path) -> GoldSet:
    try:
        return read_gold(path)
    except GoldValidationError as exc:
        raise EvaluationError(
            "GOLD_INVALID",
            f"人工核定文件未通过校验（共 {len(exc.problems)} 处问题）：{exc.problems[0].message}"
            if exc.problems
            else "人工核定文件未通过校验",
            details={
                "problems": [
                    {"code": issue.code, "message": issue.message, "source_row": issue.source_row}
                    for issue in exc.problems
                ]
            },
        ) from exc
    except GoldError as exc:
        raise EvaluationError("GOLD_UNREADABLE", f"无法读取人工核定文件：{exc}") from exc


def _load_manifest(path: Path) -> SplitManifest:
    try:
        return read_manifest(path)
    except SplitError as exc:
        raise EvaluationError(exc.code, exc.message) from exc


def _batch_ids(
    connection: sqlite3.Connection, run_id: str
) -> tuple[tuple[str, ...], dict[str, records_repo.RecordRow]]:
    """批次**有效记录**的编号（输入失败行不属于人工核定范围）。

    返回编号序列与 ``编号 → 记录`` 映射。编号缺失的有效记录单独报错：它们无法与
    核定文件对上，而「对不上」正是本闸门要发现的事。
    """
    rows = records_repo.list_records(connection, run_id)
    ids: list[str] = []
    by_id: dict[str, records_repo.RecordRow] = {}
    for row in rows:
        if row.status == "input_invalid":
            continue
        if row.record_id is None:
            raise EvaluationError(
                "RECORD_ID_MISSING",
                f"批次里有 1 条有效记录没有编号（第 {row.source_row} 行），"
                "无法与人工核定文件对应；请补齐编号后重新建批次",
                details={"source_row": row.source_row},
            )
        ids.append(row.record_id)
        by_id[row.record_id] = row
    return tuple(ids), by_id


def _check_id_sets(
    batch_ids: Sequence[str], manifest: SplitManifest, split: str
) -> tuple[str, ...]:
    """编号集合同构校验；不一致即拒绝，并指出跨 split 的编号。"""
    expected = manifest.ids_for(split)
    missing, extra = compare_ids(expected, batch_ids)

    others = tuple(
        record_id
        for record_id in extra
        if manifest.assignments.get(record_id) in SPLITS
    )
    unassigned = tuple(
        record_id for record_id in extra if record_id not in manifest.assignments
    )

    if not missing and not extra:
        return expected

    parts: list[str] = []
    if missing:
        parts.append(f"清单里有而批次没有 {len(missing)} 条：{'、'.join(missing[:10])}")
    if others:
        parts.append(
            f"批次里有 {len(others)} 条属于**另一侧**的编号：{'、'.join(others[:10])}"
            "——这会把未被使用的一侧带进本次评估，请用清单生成的子集文件建批次"
        )
    if unassigned:
        parts.append(
            f"批次里有 {len(unassigned)} 条不在划分清单中：{'、'.join(unassigned[:10])}"
        )
    raise EvaluationError(
        "ID_SET_MISMATCH",
        f"批次编号集合与划分清单的「{split}」不一致：{'；'.join(parts)}",
        details={
            "missing": list(missing),
            "extra": list(extra),
            "other_split": list(others),
            "unassigned": list(unassigned),
        },
    )


def freeze_list(
    run: runs_repo.RunRow,
    gold: GoldSet,
    manifest: SplitManifest,
    *,
    manifest_name: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    """§0.3 第 8 条的冻结清单：这批评测结论到底属于哪个版本。

    清单自身没有名字，只有它记录的 gold 文件名，因此 `manifest_name` 由调用方给出
    （磁盘上那个文件名）；摘要按**文件字节**算，与 `gold.sha256` 同一算法。
    """
    versions = run.versions
    return {
        "program": versions.get("program") or package_version(),
        "stage1_prompt_version": versions.get("stage1_prompt"),
        "stage2_prompt_version": versions.get("stage2_prompt"),
        "stage1_schema_version": versions.get("stage1_schema"),
        "stage2_schema_version": versions.get("stage2_schema"),
        "input_contract_version": versions.get("input_contract"),
        "model_config": model_config_snapshot(run.config_snapshot),
        "gold": {
            "filename": gold.path.name,
            "data_version": gold.metadata.data_version,
            "contract_version": gold.contract_version,
            "sha256": gold.sha256,
            "sha256_prefix": gold.sha256_prefix,
            "annotated_by": gold.metadata.annotated_by,
            "annotated_on": gold.metadata.annotated_on,
            "source_sha256_prefix": gold.metadata.source_sha256_prefix,
        },
        "manifest": {
            "filename": manifest_name,
            "version": manifest.manifest_version,
            "sha256": manifest_sha256,
            "seed": manifest.seed,
            # 两份摘要相同才继续往下算，所以这个字段恒为真；它在这里的意义是
            # 「这条评估记录当时核对过」，而不是一个可能失败的判断。
            "gold_sha256_match": manifest.gold_sha256 == gold.sha256,
        },
        "frozen_at": utc_now(),
    }


def split_info_of(gold: GoldSet, manifest: SplitManifest, split: str) -> dict[str, Any]:
    """落库的「划分清单要点」：报告与页面据此复原划分，不依赖清单文件仍在磁盘上。"""
    counts = manifest.counts if isinstance(manifest.counts, dict) else {}
    return {
        "split": split,
        "seed": manifest.seed,
        "manifest_version": manifest.manifest_version,
        "calibration_target": manifest.calibration_target,
        "gold_filename": gold.path.name,
        "gold_data_version": gold.metadata.data_version,
        "gold_sha256_prefix": gold.sha256_prefix,
        "calibration": len(manifest.ids_for(CALIBRATION)),
        "holdout": len(manifest.ids_for(HOLDOUT)),
        "calibration_by_label": counts.get("calibration_by_label", {}),
        "holdout_by_label": counts.get("holdout_by_label", {}),
        "forced_calibration": list(manifest.forced_calibration),
        "excluded_ids": list(counts.get("excluded_ids", [])),
        "excluded_reasons": dict(counts.get("excluded_reasons", {})),
        "selected_ids": list(manifest.ids_for(split)),
    }


def _scored_records(
    ids: Sequence[str],
    by_id: dict[str, records_repo.RecordRow],
    gold: GoldSet,
) -> tuple[ScoredRecord, ...]:
    """按**核定文件顺序**组装待评分记录。

    顺序固定成 gold 顺序（即清单 assignments 的顺序），报告的逐条明细、页面的
    逐条对照与 `evaluation_records` 的写入顺序因此全都一致。

    没有预测的记录照样进来：它们会进 `missing_ids` 并拉低分类完成率，这是
    §0.3 第 5 条要求「单独报告、不静默丢弃」的那一批。
    """
    label_of = {entry.record_id: entry.label for entry in gold.included}
    scored: list[ScoredRecord] = []
    for record_id in ids:
        # 清单里的编号必须能在核定文件里找到人工标签：清单是照着它划的，找不到
        # 说明清单被改过（摘要相同但内容不同），这时算出来的分数没有依据。
        label = label_of.get(record_id)
        if label is None:
            raise EvaluationError(
                "GOLD_MISMATCH",
                f"划分清单里的编号 {record_id} 在人工核定文件中没有可计分的人工标签；"
                "请用清单生成时的同一份核定文件重新评估",
                details={"record_id": record_id},
            )
        row = by_id[record_id]
        agent_label = row.final_label if row.final_label in LABELS else None
        scored.append(
            ScoredRecord(
                record_id=record_id,
                gold_label=label,
                agent_label=agent_label,
                review_required=(
                    None if row.review_required is None else bool(row.review_required)
                ),
                status=row.status,
                failure_stage=row.failure_stage,
            )
        )
    return tuple(scored)


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

_CONFUSION_HEADER = ("人工真值 ＼ agent 预测", *LABELS)


def _confusion_rows(metrics: EvaluationMetrics) -> list[list[str]]:
    rows: list[list[str]] = []
    for truth in LABELS:
        cells = metrics.confusion[truth]
        total = sum(cells.values())
        rows.append([truth, *[str(cells[label]) for label in LABELS], str(total)])
    totals = [
        sum(metrics.confusion[truth][label] for truth in LABELS) for label in LABELS
    ]
    rows.append(["合计", *[str(value) for value in totals], str(sum(totals))])
    return rows


def render_report_text(
    *,
    evaluation_id: str,
    run: runs_repo.RunRow,
    split: str,
    created_at: str,
    gold: GoldSet,
    manifest: SplitManifest,
    manifest_name: str,
    freeze: dict[str, Any],
    metrics: EvaluationMetrics,
    checks: Sequence[ThresholdCheck],
) -> str:
    """人读报告（Markdown）；与 JSON 报告同一批数字，便于人工核对与存档。"""
    lines: list[str] = []
    lines.append(f"# 评估报告 {evaluation_id}")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 批次 | `{run.run_id}`（{run.source_filename}）|")
    lines.append(f"| 一侧 | {split} |")
    lines.append(f"| 评估时间 | {created_at} |")
    lines.append(f"| 人工核定文件 | `{gold.path.name}`（{gold.metadata.data_version}，sha256 {gold.sha256_prefix}…）|")
    lines.append(f"| 划分清单 | `{manifest_name}`（seed {manifest.seed}，sha256 {freeze['manifest']['sha256'][:12]}…）|")
    lines.append(f"| 参与评分 | {metrics.valid_total} 条（有预测 {metrics.scored_total - len(metrics.missing_ids)} 条）|")
    lines.append(f"| 人工排除 | {len(metrics.excluded_ids)} 条 |")
    lines.append("")

    lines.append("## 达标判定")
    lines.append("")
    lines.append("| 项 | 要求 | 实际 | 结论 |")
    lines.append("| --- | --- | --- | --- |")
    for check in checks:
        verdict = "不可计算" if check.passed is None else ("达标" if check.passed else "未达标")
        lines.append(f"| {check.name} | {check.requirement} | {check.actual} | {verdict} |")
    lines.append("")

    lines.append("## 混淆矩阵")
    lines.append("")
    lines.append("行 = 人工真值，列 = agent 原始预测。没有预测的记录不在矩阵内。")
    lines.append("")
    lines.append("| " + " | ".join(_CONFUSION_HEADER) + " | 合计 |")
    lines.append("| " + " | ".join("---" for _ in range(len(_CONFUSION_HEADER) + 1)) + " |")
    for row in _confusion_rows(metrics):
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## 逐类指标")
    lines.append("")
    lines.append("| 类别 | TP | FP | FN | 精确率 | 召回率 | F1 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for label in LABELS:
        cell = metrics.per_class[label]
        lines.append(
            f"| {label} | {cell.tp} | {cell.fp} | {cell.fn} | {cell.precision.display()} "
            f"| {cell.recall.display()} | {cell.f1.display()} |"
        )
    lines.append("")

    lines.append("## 总体指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| Macro-F1 | {metrics.macro_f1.display()} |")
    lines.append(f"| 总体一致率 | {metrics.agreement.display()} |")
    lines.append(f"| 非正确类误判正确率 | {metrics.wrong_as_correct.display()} |")
    lines.append(f"| 分类完成率 | {metrics.completion.display()} |")
    lines.append(f"| 复核比例 | {metrics.review_ratio.display()} |")
    lines.append("")

    lines.append("## 未参与评分的记录")
    lines.append("")
    if metrics.missing_ids:
        lines.append(
            f"- 技术失败（无效尝试/服务错误）{len(metrics.failed_ids)} 条："
            + _join_or_none(metrics.failed_ids)
        )
        lines.append(
            f"- 未完成（还没跑到分类完成）{len(metrics.unfinished_ids)} 条："
            + _join_or_none(metrics.unfinished_ids)
        )
        lines.append("")
        lines.append("这些记录**不进混淆矩阵**，只拉低分类完成率；它们不是「判错」。")
    else:
        lines.append("- 无：全部有效记录都已分类。")
    lines.append("")

    excluded = [entry for entry in gold.excluded]
    lines.append("## 人工排除的记录")
    lines.append("")
    if excluded:
        lines.append("| 编号 | 排除理由 |")
        lines.append("| --- | --- |")
        for entry in excluded:
            lines.append(f"| {entry.record_id} | {entry.exclusion_reason or ''} |")
    else:
        lines.append("- 无。")
    lines.append("")

    lines.append("## 冻结清单")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 程序版本 | {freeze['program']} |")
    lines.append(f"| 阶段一提示词 | {freeze['stage1_prompt_version']} |")
    lines.append(f"| 阶段二提示词 | {freeze['stage2_prompt_version']} |")
    lines.append(f"| 阶段一结果结构 | {freeze['stage1_schema_version']} |")
    lines.append(f"| 阶段二结果结构 | {freeze['stage2_schema_version']} |")
    lines.append(f"| 输入合同 | {freeze['input_contract_version']} |")
    lines.append(f"| 冻结时间 | {freeze['frozen_at']} |")
    lines.append("")

    lines.append("## 逐条对照")
    lines.append("")
    lines.append("| 编号 | 人工标签 | agent 原预测 | 是否一致 | 需复核 | 记录状态 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for record in metrics.scored:
        if record.agent_label is None:
            agree = "无预测"
        else:
            agree = "一致" if record.agent_label == record.gold_label else "不一致"
        review = "—" if record.review_required is None else ("是" if record.review_required else "否")
        lines.append(
            f"| {record.record_id} | {record.gold_label} | {record.agent_label or '—'} "
            f"| {agree} | {review} | {record.status} |"
        )
    lines.append("")
    lines.append("> 本报告由 `python -m aidhu_om_agent evaluate` 生成；计分只用 agent 原始预测，")
    lines.append("> 人工修订不计作预测。生成过程不调用任何模型。")
    lines.append("")
    return "\n".join(lines)


def _join_or_none(ids: Sequence[str]) -> str:
    return "、".join(ids) if ids else "无"


def _report_payload(
    *,
    evaluation_id: str,
    run: runs_repo.RunRow,
    split: str,
    created_at: str,
    gold: GoldSet,
    manifest: SplitManifest,
    manifest_name: str,
    manifest_sha256: str,
    info: dict[str, Any],
    freeze: dict[str, Any],
    metrics: EvaluationMetrics,
    checks: Sequence[ThresholdCheck],
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """JSON 报告：自洽到「清单文件被删掉也读得懂」的程度。"""
    return {
        "evaluation_id": evaluation_id,
        "run_id": run.run_id,
        "split": split,
        "created_at": created_at,
        "gold": {
            "filename": gold.path.name,
            "sha256": gold.sha256,
            "data_version": gold.metadata.data_version,
            "contract_version": gold.contract_version,
            "annotated_by": gold.metadata.annotated_by,
            "annotated_on": gold.metadata.annotated_on,
            "sheet": gold.sheet_name,
        },
        "manifest": {
            "filename": manifest_name,
            "sha256": manifest_sha256,
            "version": manifest.manifest_version,
            "seed": manifest.seed,
        },
        "split_info": info,
        "freeze": freeze,
        "metrics": metrics.as_dict(),
        "thresholds": [check.as_dict() for check in checks],
        "warnings": [
            {"code": issue.code, "message": issue.message, "source_row": issue.source_row}
            for issue in gold.warnings
        ],
        "records": list(records),
    }


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def run_evaluation(
    database: Database,
    *,
    run_id: str,
    gold_path: str | Path,
    manifest_path: str | Path,
    split: str,
    config: AppConfig,
) -> EvaluationOutcome:
    """评估一个批次的一条 split；成功的评估**总是**留痕（库 + 两份报告）。

    被闸门拒绝时不写任何东西：拒绝不是一次评估，落库会让「评估历史」里出现
    一条没有结论的记录。
    """
    if split not in SPLITS:
        raise EvaluationError(
            "SPLIT_UNKNOWN",
            f"未知的 split 名 {split!r}；只接受 {'／'.join(SPLITS)}",
            http_status=422,
            details={"allowed": list(SPLITS)},
        )

    gold_file = Path(gold_path)
    manifest_file = Path(manifest_path)
    if not gold_file.is_file():
        raise EvaluationError("GOLD_UNREADABLE", f"找不到人工核定文件：{gold_file}")
    if not manifest_file.is_file():
        raise EvaluationError("SPLIT_MANIFEST_MISSING", f"找不到划分清单：{manifest_file}")

    gold = _load_gold(gold_file)
    manifest = _load_manifest(manifest_file)

    # 清单记的是它自己那一份 gold 的摘要；两者不同就说明清单不是为这份答案划的，
    # 「编号该在哪一侧」这个判断本身就不成立，因此不继续往下算。
    if manifest.gold_sha256 != gold.sha256:
        raise EvaluationError(
            "GOLD_MISMATCH",
            f"划分清单对应的人工核定文件与本文件不是同一份"
            f"（清单 {manifest.gold_sha256[:12]}…，本次 {gold.sha256[:12]}…）；"
            "请用清单生成时的同一份 gold 重新导出，或重新生成划分清单",
            details={
                "manifest_gold_sha256": manifest.gold_sha256,
                "gold_sha256": gold.sha256,
            },
        )

    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            run = runs_repo.get_run(snapshot, run_id)
            if run is None:
                raise EvaluationError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)
            if run.status not in TERMINAL_RUN_STATUSES:
                raise EvaluationError(
                    "RUN_NOT_FINISHED",
                    f"批次 {run_id} 还在执行（status={run.status}），现在评估得到的是一份"
                    "立刻过期的报告；请等它到达终态后再评估",
                    details={"status": run.status},
                )
            batch_ids, by_id = _batch_ids(snapshot, run_id)
            _check_id_sets(batch_ids, manifest, split)

        ids = manifest.ids_for(split)
        scored = _scored_records(ids, by_id, gold)
        metrics = compute_metrics(
            scored, excluded_ids=tuple(entry.record_id for entry in gold.excluded)
        )
        checks = check_thresholds(metrics)

        manifest_sha256 = file_digest(manifest_file)
        freeze = freeze_list(
            run,
            gold,
            manifest,
            manifest_name=manifest_file.name,
            manifest_sha256=manifest_sha256,
        )
        info = split_info_of(gold, manifest, split)

        records_payload = [
            {
                "record_id": record.record_id,
                "record_key": by_id[record.record_id].record_key,
                "gold_label": record.gold_label,
                "agent_label": record.agent_label,
                "agree": (
                    None if record.agent_label is None else record.agent_label == record.gold_label
                ),
                "review_required": record.review_required,
                "record_status": record.status,
                "failure_stage": record.failure_stage,
            }
            for record in metrics.scored
        ]

        evaluation_id = evaluations_repo.new_evaluation_id()
        created_at = utc_now()
        report_json_name = f"evaluation-{evaluation_id}-{split}.json"
        report_text_name = f"evaluation-{evaluation_id}-{split}.md"

        target_dir = reports_dir(config)
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = _report_payload(
            evaluation_id=evaluation_id,
            run=run,
            split=split,
            created_at=created_at,
            gold=gold,
            manifest=manifest,
            manifest_name=manifest_file.name,
            manifest_sha256=manifest_sha256,
            info=info,
            freeze=freeze,
            metrics=metrics,
            checks=checks,
            records=records_payload,
        )
        report_json_path = target_dir / report_json_name
        report_text_path = target_dir / report_text_name
        # 先落盘再落库：库里的两列是报告文件名，反过来会出现「记录指向不存在的
        # 文件」。文件先写、库后写的最坏情况只留下一份孤儿报告，重跑即可覆盖。
        report_json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report_text_path.write_text(
            render_report_text(
                evaluation_id=evaluation_id,
                run=run,
                split=split,
                created_at=created_at,
                gold=gold,
                manifest=manifest,
                manifest_name=manifest_file.name,
                freeze=freeze,
                metrics=metrics,
                checks=checks,
            ),
            encoding="utf-8",
        )

        with write_transaction(connection) as writer:
            evaluations_repo.insert_evaluation(
                writer,
                run_id=run_id,
                split=split,
                gold={
                    "filename": gold.path.name,
                    "sha256": gold.sha256,
                    "data_version": gold.metadata.data_version,
                    "contract_version": gold.contract_version,
                },
                manifest={
                    "filename": manifest_file.name,
                    "sha256": freeze["manifest"]["sha256"],
                    "version": manifest.manifest_version,
                    "seed": manifest.seed,
                },
                split_info=info,
                versions=freeze,
                metrics=metrics.as_dict(),
                thresholds=[check.as_dict() for check in checks],
                report_json_name=report_json_name,
                report_text_name=report_text_name,
                valid_count=metrics.valid_total,
                scored_count=len(metrics.scored) - len(metrics.missing_ids),
                excluded_count=len(gold.excluded),
                records=records_payload,
                evaluation_id=evaluation_id,
                created_at=created_at,
            )
    finally:
        connection.close()

    return EvaluationOutcome(
        evaluation_id=evaluation_id,
        run_id=run_id,
        split=split,
        metrics=metrics,
        checks=checks,
        report_json_path=report_json_path,
        report_text_path=report_text_path,
        warnings=tuple(issue.message for issue in gold.warnings),
    )


# --------------------------------------------------------------------------
# 只读视图（界面 / API 用）
# --------------------------------------------------------------------------


def _evaluation_summary(row: evaluations_repo.EvaluationRow) -> dict[str, Any]:
    metrics = row.metrics
    return {
        "evaluation_id": row.evaluation_id,
        "run_id": row.run_id,
        "split": row.split,
        "created_at": row.created_at,
        "gold_filename": row.gold_filename,
        "gold_data_version": row.gold_data_version,
        "manifest_sha256": row.manifest_sha256,
        "valid_count": row.valid_count,
        "scored_count": row.scored_count,
        "excluded_count": row.excluded_count,
        "macro_f1": metrics.get("macro_f1", {}).get("display"),
        "agreement": metrics.get("agreement", {}).get("display"),
        "wrong_as_correct": metrics.get("wrong_as_correct", {}).get("display"),
        "completion": metrics.get("completion", {}).get("display"),
        "passed": _checks_passed(row.thresholds),
        "report_text_name": row.report_text_name,
    }


def _checks_passed(thresholds: Sequence[dict[str, Any]]) -> bool:
    """达标判定汇总；不可计算（``None``）不算通过。"""
    return all(item.get("passed") is True for item in thresholds)


def list_run_evaluations(
    database: Database,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = EVALUATION_PAGE_SIZE_DEFAULT,
) -> dict[str, Any]:
    """`GET /api/runs/{run_id}/evaluations`：某批次的评估历史（新→旧）。"""
    if page < 1 or page_size < 1 or page_size > EVALUATION_PAGE_SIZE_MAX:
        raise EvaluationError(
            "PARAM_VALIDATION",
            f"page ≥ 1 且 page_size 在 1—{EVALUATION_PAGE_SIZE_MAX} 之间",
            http_status=422,
        )
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            run = runs_repo.get_run(snapshot, run_id)
            if run is None:
                raise EvaluationError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)
            rows, total = evaluations_repo.list_evaluations_page(
                snapshot, run_id, limit=page_size, offset=(page - 1) * page_size
            )
            items = [_evaluation_summary(row) for row in rows]
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    finally:
        connection.close()


def _require_evaluation(
    connection: sqlite3.Connection, evaluation_id: str
) -> evaluations_repo.EvaluationRow:
    row = evaluations_repo.get_evaluation(connection, evaluation_id)
    if row is None:
        raise EvaluationError(
            "NOT_FOUND", f"未知 evaluation_id：{evaluation_id}", http_status=404
        )
    return row


def evaluation_detail(
    database: Database, evaluation_id: str, *, config: AppConfig
) -> dict[str, Any]:
    """`GET /api/evaluations/{evaluation_id}`：指标、达标判定、划分要点与报告。

    **报告正文直接内嵌**：评估报告不是导出产物（`artifacts.export_id` 非空，走不了
    下载接口），而它只是几十 KB 的文本，内嵌后页面可以用 Blob 直接下载，不新增
    第四个接口。文件路径与是否存在一并返回，便于排查「文件被手工删了」。
    """
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            row = _require_evaluation(snapshot, evaluation_id)
            run = runs_repo.get_run(snapshot, row.run_id)
            report_dir = reports_dir(config)
            text_path = report_dir / row.report_text_name
            json_path = report_dir / row.report_json_name
            report_text = text_path.read_text(encoding="utf-8") if text_path.is_file() else None
            report_files = [
                {
                    "name": row.report_text_name,
                    "kind": "text",
                    "exists": text_path.is_file(),
                    "size_bytes": text_path.stat().st_size if text_path.is_file() else None,
                },
                {
                    "name": row.report_json_name,
                    "kind": "json",
                    "exists": json_path.is_file(),
                    "size_bytes": json_path.stat().st_size if json_path.is_file() else None,
                },
            ]

        return {
            "evaluation_id": row.evaluation_id,
            "run_id": row.run_id,
            "split": row.split,
            "created_at": row.created_at,
            "gold": {
                "filename": row.gold_filename,
                "sha256": row.gold_sha256,
                "sha256_prefix": row.gold_sha256[:12],
                "data_version": row.gold_data_version,
                "contract_version": row.gold_contract_version,
            },
            "manifest": {
                "filename": row.manifest_filename,
                "sha256": row.manifest_sha256,
                "sha256_prefix": row.manifest_sha256[:12],
                "version": row.manifest_version,
                "seed": row.seed,
            },
            "run": None
            if run is None  # pragma: no cover - 外键保证存在
            else {
                "run_id": run.run_id,
                "source_filename": run.source_filename,
                "status": run.status,
                "revision": run.revision,
                "total_count": run.total_count,
                "valid_count": run.valid_count,
            },
            "split_info": row.split_info,
            "freeze": row.versions,
            "metrics": row.metrics,
            "thresholds": list(row.thresholds),
            "counts": {
                "valid": row.valid_count,
                "scored": row.scored_count,
                "excluded": row.excluded_count,
                "missing": row.valid_count - row.scored_count,
            },
            "report_files": report_files,
            "report_text": report_text,
        }
    finally:
        connection.close()


def list_evaluation_records(
    database: Database,
    evaluation_id: str,
    *,
    page: int = 1,
    page_size: int = RECORD_PAGE_SIZE_DEFAULT,
    agree: bool | None = None,
    status: str | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    """`GET /api/evaluations/{evaluation_id}/records`：逐条对照的分页列表。

    ``agree=false`` **只命中「有预测但不一致」**的行；没有预测的行 `agree` 是 NULL，
    要单独看（`status` 筛 `failed`，或看 `missing`）。把两类混在一起会让人以为
    「不一致的都是判错了」。
    """
    if page < 1 or page_size < 1 or page_size > RECORD_PAGE_SIZE_MAX:
        raise EvaluationError(
            "PARAM_VALIDATION",
            f"page ≥ 1 且 page_size 在 1—{RECORD_PAGE_SIZE_MAX} 之间",
            http_status=422,
        )
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            _require_evaluation(snapshot, evaluation_id)
            rows, total = evaluations_repo.list_evaluation_records(
                snapshot,
                evaluation_id,
                limit=page_size,
                offset=(page - 1) * page_size,
                agree=agree,
                record_status=status,
                record_id=record_id,
            )
        return {
            "items": [
                {
                    "record_id": row.record_id,
                    "record_key": row.record_key,
                    "gold_label": row.gold_label,
                    "agent_label": row.agent_label,
                    "agree": None if row.agree is None else bool(row.agree),
                    "review_required": (
                        None if row.review_required is None else bool(row.review_required)
                    ),
                    "record_status": row.record_status,
                    "failure_stage": row.failure_stage,
                }
                for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    finally:
        connection.close()


__all__ = [
    "EVALUATION_PAGE_SIZE_DEFAULT",
    "EVALUATION_PAGE_SIZE_MAX",
    "RECORD_PAGE_SIZE_DEFAULT",
    "RECORD_PAGE_SIZE_MAX",
    "TERMINAL_RUN_STATUSES",
    "EvaluationError",
    "EvaluationOutcome",
    "evaluation_detail",
    "evaluation_root",
    "freeze_list",
    "list_evaluation_records",
    "list_run_evaluations",
    "render_report_text",
    "reports_dir",
    "run_evaluation",
    "split_info_of",
]
