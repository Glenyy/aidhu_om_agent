"""S07 的**一条可评估链路**：核定文件 → 划分清单 → 子集工作簿 → 批次 → 原预测。

放在 `tests/fixtures/` 而不是某个测试文件里，是因为单测与接口集成测试都要用它
（测试目录各自入 `sys.path`，跨目录 import 测试模块在只跑一个目录时会失败）。

这里**只造数据、不判对错**：走的是真实服务（`precheck` → `UploadStore` →
`create_run` → `records_repo` 写预测），不是往库里硬塞行。因此任何一条链路上的
契约变了，用它的测试都会跟着红——这正是要的效果。

合成核定文件的编号是 1..N（三类连续），所以必然含强制进校准集的 `1` 与 `4`。
手算的划分结果（种子固定、33 条 = 15／10／8）：校准 **18** 条 = 10／5／3，
保留 **15** 条 = 5／5／5。注意校准集「每类 ≥5」**不成立**（正确资料但回答错误
只有 3 条）——「每类 ≥5」是保留集的门槛。

全程合成数据、模拟预测，**零真实模型调用**。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fixtures.gold_samples import METADATA, gold_row, write_synthetic_gold

from aidhu_om_agent.config import (
    AppConfig,
    ExecutionConfig,
    LimitsConfig,
    ModelConfig,
    PathsConfig,
    load_config,
)
from aidhu_om_agent.evaluation.gold import GoldSet, read_gold
from aidhu_om_agent.evaluation.split import (
    CALIBRATION,
    HOLDOUT,
    manifest_payload,
    plan_split,
    write_manifest,
    write_subset_workbook,
)
from aidhu_om_agent.excel.reader import precheck
from aidhu_om_agent.repositories import records as records_repo
from aidhu_om_agent.repositories import runs as runs_repo
from aidhu_om_agent.schemas.judgement import LABELS
from aidhu_om_agent.services.batches import create_run
from aidhu_om_agent.services.evaluation import EvaluationOutcome, run_evaluation
from aidhu_om_agent.services.uploads import UploadStore
from aidhu_om_agent.storage import Database, utc_now, write_transaction

CORRECT, NO_REF, WRONG = LABELS

#: 合成核定文件的每类条数：三类都远超「校准 ≥1 + 保留 ≥5」，足以走通划分。
COUNTS = {CORRECT: 15, NO_REF: 10, WRONG: 8}
TOTAL = sum(COUNTS.values())

#: 手算的划分结果：校准 18 条与保留 15 条的逐类条数。
CALIBRATION_PER_CLASS = {CORRECT: 10, NO_REF: 5, WRONG: 3}
HOLDOUT_PER_CLASS = {CORRECT: 5, NO_REF: 5, WRONG: 5}
CALIBRATION_SIZE = sum(CALIBRATION_PER_CLASS.values())
HOLDOUT_SIZE = sum(HOLDOUT_PER_CLASS.values())

#: 一个「答案函数」：给定 (编号, 人工标签)，返回 (状态, 预测标签)。
Answer = Callable[[str, str], "tuple[str, str | None]"]


def evaluation_config(root: Path) -> AppConfig:
    """把库、上传目录、产物目录全部圈在 ``root`` 下，不碰开发机的真实 `data/`。

    与 `test_batches.make_config` 同一形状，但**不带凭据**：本文件只做评估，
    没有任何路径会读 `api_key`。
    """
    model = ModelConfig(
        base_url="https://example.invalid/v1",
        model="test-model",
        timeout_seconds=1.0,
        api_key=None,
    )
    return AppConfig(
        stage1=model,
        stage2=model,
        execution=ExecutionConfig(
            concurrency=1, max_attempts_per_stage_campaign=3, retry_backoff_seconds=(0, 0)
        ),
        limits=LimitsConfig(max_upload_bytes=4 * 1024 * 1024, max_records=1000),
        paths=PathsConfig(
            database=root / "runtime" / "state.sqlite3",
            uploads=root / "runtime" / "uploads",
            runtime=root / "runtime",
            outputs=root / "outputs",
            logs=root / "logs",
            frontend_dist=root / "dist",
        ),
        source_config=root / "config.toml",
        source_env=None,
    )


def make_gold(tmp_path: Path, counts: dict[str, int], name: str = "gold.xlsx") -> GoldSet:
    """按「每类条数」构造合成核定文件；编号为 1..N，因此含强制编号 1 与 4。"""
    rows: list[dict[str, object]] = []
    index = 0
    for label in LABELS:
        for _ in range(counts[label]):
            index += 1
            rows.append(
                gold_row(
                    str(index),
                    judgement=label,
                    reason=f"合成理由 {index}",
                    refs=("合成资料。",) if label != NO_REF else (None,),
                )
            )
    return read_gold(write_synthetic_gold(tmp_path / name, rows, metadata=METADATA))


# --------------------------------------------------------------------------
# 答案函数：把「预测」写成确定性、可手算的形式
# --------------------------------------------------------------------------


def predict_all_correct(record_id: str, label: str) -> tuple[str, str | None]:
    """默认预测：全部判对。"""
    return "completed", label


def flip_nth(per_label: dict[str, tuple[int, str]]) -> Answer:
    """构造「把某类的前 N 条判成另一个标签」的答案函数。

    ``{CORRECT: (2, NO_REF)}`` 表示：前 2 条人工标签为 CORRECT 的记录判成 NO_REF。
    计数按**工作簿顺序**（= 划分清单里该侧的编号顺序）推进，与随机洗牌无关。
    """
    seen = {label: 0 for label in per_label}

    def answer(record_id: str, label: str) -> tuple[str, str | None]:
        if label in per_label:
            limit, replacement = per_label[label]
            if seen[label] < limit:
                seen[label] += 1
                return "completed", replacement
        return "completed", label

    return answer


def fail_nth(label: str, position: int) -> Answer:
    """构造「某类的第 position 条技术失败」的答案函数（1 起数）。"""
    seen = 0

    def answer(record_id: str, gold_label: str) -> tuple[str, str | None]:
        nonlocal seen
        if gold_label == label:
            seen += 1
            if seen == position:
                return "failed", None
        return "completed", gold_label

    return answer


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    """一条可评估的合成链路。"""

    config: AppConfig
    database: Database
    gold: GoldSet
    manifest_path: Path
    run_id: str
    split: str
    ids: tuple[str, ...]

    def by_class(self) -> dict[str, list[str]]:
        """本次评估的编号按人工标签分组，保持清单顺序。"""
        label_of = {entry.record_id: entry.label for entry in self.gold.included}
        grouped: dict[str, list[str]] = {label: [] for label in LABELS}
        for record_id in self.ids:
            grouped[label_of[record_id]].append(record_id)  # type: ignore[index]
        return grouped

    def evaluate(self, **overrides: object) -> EvaluationOutcome:
        """按本场景的参数调一次 `run_evaluation`；``overrides`` 覆盖单个参数。

        闸门类测试靠它把参数改坏（换成别的 split、指向不存在的文件、拿另一侧的
        批次去评），正常路径则一个参数都不用给。
        """
        call: dict[str, object] = {
            "run_id": self.run_id,
            "gold_path": self.gold.path,
            "manifest_path": self.manifest_path,
            "split": self.split,
        }
        call.update(overrides)
        return run_evaluation(self.database, config=self.config, **call)  # type: ignore[arg-type]


def subdir(root: Path, name: str) -> Path:
    """同一个 `tmp_path` 下要放多份库与文件时，各占一个子目录。"""
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    return target


def seed_scenario(
    root: Path,
    *,
    answers: Answer | None = None,
    split: str = CALIBRATION,
    review_ids: tuple[str, ...] = (),
    counts: dict[str, int] | None = None,
    run_status: str = "completed",
    batch_split: str | None = None,
    drop_ids: int = 0,
    config_path: Path | None = None,
) -> Scenario:
    """按真实服务链路造一个批次，并把预测与批次终态写进库。

    ``batch_split`` 与 ``drop_ids`` 用来**故意造出不一致的批次**：前者是跨侧泄漏
    （拿另一侧的编号建批次），后者是缺编号。两者都应被闸门拦住。

    ``config_path`` 给出时用 `load_config` 读它（CLI 测试需要配置文件本身）；
    省略则用 `evaluation_config(root)` 的内存配置。
    """
    config = load_config(config_path) if config_path is not None else evaluation_config(root)

    gold = make_gold(root, counts or COUNTS)
    plan = plan_split(gold)
    manifest_path = write_manifest(
        root / "split-v1.json",
        manifest_payload(plan, gold, generated_at="2026-10-07T00:00:00+00:00"),
    )

    source_ids = plan.ids_for(batch_split or split)
    if drop_ids:
        source_ids = source_ids[:-drop_ids]
    by_id = {entry.record_id: entry for entry in gold.entries}
    subset = write_subset_workbook(
        root / "batch.xlsx", [by_id[record_id] for record_id in source_ids]
    )

    database = Database(config.paths.database)
    database.initialize()
    store = UploadStore(database, config.paths.uploads)
    with subset.open("rb") as handle:
        upload = store.save(subset.name, handle, limits=config.limits)
    parsed = precheck(upload.path, None, limits=config.limits)
    assert parsed.report.status == "passed", parsed.report.blockers
    validation = store.put_validation(upload.upload_id, parsed)
    created = create_run(database, config, validation_id=validation.validation_id)

    apply_predictions(
        database,
        created.run_id,
        labels={entry.record_id: entry.label for entry in gold.included},
        answers=answers or predict_all_correct,
        review_ids=review_ids,
        run_status=run_status,
    )

    return Scenario(
        config=config,
        database=database,
        gold=gold,
        manifest_path=manifest_path,
        run_id=created.run_id,
        split=split,
        ids=plan.ids_for(split),
    )


def apply_predictions(
    database: Database,
    run_id: str,
    *,
    labels: dict[str, str | None],
    answers: Answer,
    review_ids: tuple[str, ...] = (),
    run_status: str = "completed",
) -> None:
    """把预测与批次终态写进库；形状与 worker 提交时一致（只有 ``completed`` 有标签）。

    迭代顺序是 `order_index`，也就是工作簿顺序——答案函数依赖这一点来数「前 N 条」。
    """
    connection = database.connect()
    try:
        with write_transaction(connection) as writer:
            for row in records_repo.list_records(writer, run_id):
                assert row.record_id is not None
                status, predicted = answers(row.record_id, labels[row.record_id] or "")
                if status == "completed":
                    records_repo.mark_completed(
                        writer,
                        row.record_key,
                        final_label=predicted,
                        review_required=row.record_id in review_ids,
                        updated_at=utc_now(),
                    )
                else:
                    records_repo.mark_failed(
                        writer,
                        row.record_key,
                        failure_stage=2,
                        failure={"code": "SERVICE_ERROR", "message": "合成技术失败"},
                        updated_at=utc_now(),
                    )
            runs_repo.finish_run(
                writer, run_id=run_id, status=run_status, finished_at=utc_now()
            )
    finally:
        connection.close()


def count_rows(database: Database, table: str) -> int:
    """某张表的行数；用来证明「评估零模型调用」与「没多出评估行」。"""
    connection = database.connect()
    try:
        return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    finally:
        connection.close()


def reports_of(config: AppConfig) -> Path:
    """配置指向的数据目录下的报告目录 `<database 父目录>/evaluation/reports/`。"""
    return config.paths.database.parent / "evaluation" / "reports"


__all__ = [
    "CALIBRATION",
    "CALIBRATION_PER_CLASS",
    "CALIBRATION_SIZE",
    "CORRECT",
    "COUNTS",
    "HOLDOUT",
    "HOLDOUT_PER_CLASS",
    "HOLDOUT_SIZE",
    "NO_REF",
    "Scenario",
    "TOTAL",
    "WRONG",
    "Answer",
    "apply_predictions",
    "count_rows",
    "evaluation_config",
    "fail_nth",
    "flip_nth",
    "make_gold",
    "predict_all_correct",
    "reports_of",
    "seed_scenario",
    "subdir",
]
