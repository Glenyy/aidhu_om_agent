"""评估结果的读写（S07-03）。

两张表的关系是**一次评估 + 它的逐条对照**：`evaluations` 存汇总（指标、划分要点、
版本快照、报告文件名），`evaluation_records` 存每条记录的「人工真值 / agent 原预测 /
是否一致」。页面与 CLI 报告都从这里还原当时的数字，**不重算**——重算会让「上次看到的
数字」和「这次看到的数字」因为数据变动而对不上。

写入口只有一个：`insert_evaluation`（一个短事务里写两张表）；其余都是读。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..storage.database import utc_now
from .records import new_record_key  # noqa: F401 - 便于调用方从本模块取键生成器


def new_evaluation_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class EvaluationRow:
    """`evaluations` 的一行；JSON 列在这里就解析好，调用方不再碰字符串。"""

    evaluation_id: str
    run_id: str
    split: str
    gold_filename: str
    gold_sha256: str
    gold_data_version: str
    gold_contract_version: str
    manifest_filename: str
    manifest_sha256: str
    manifest_version: str
    seed: int
    split_info: dict[str, Any]
    versions: dict[str, Any]
    metrics: dict[str, Any]
    thresholds: tuple[dict[str, Any], ...]
    report_json_name: str
    report_text_name: str
    valid_count: int
    scored_count: int
    excluded_count: int
    created_at: str


@dataclass(frozen=True)
class EvaluationRecordRow:
    """`evaluation_records` 的一行。"""

    evaluation_id: str
    record_id: str
    record_key: str | None
    gold_label: str
    agent_label: str | None
    agree: int | None
    review_required: int | None
    record_status: str
    failure_stage: str | None


def _row_to_evaluation(row: sqlite3.Row) -> EvaluationRow:
    return EvaluationRow(
        evaluation_id=row["evaluation_id"],
        run_id=row["run_id"],
        split=row["split"],
        gold_filename=row["gold_filename"],
        gold_sha256=row["gold_sha256"],
        gold_data_version=row["gold_data_version"],
        gold_contract_version=row["gold_contract_version"],
        manifest_filename=row["manifest_filename"],
        manifest_sha256=row["manifest_sha256"],
        manifest_version=row["manifest_version"],
        seed=int(row["seed"]),
        split_info=json.loads(row["split_json"]),
        versions=json.loads(row["versions_json"]),
        metrics=json.loads(row["metrics_json"]),
        thresholds=tuple(json.loads(row["thresholds_json"])),
        report_json_name=row["report_json_name"],
        report_text_name=row["report_text_name"],
        valid_count=int(row["valid_count"]),
        scored_count=int(row["scored_count"]),
        excluded_count=int(row["excluded_count"]),
        created_at=row["created_at"],
    )


def _row_to_evaluation_record(row: sqlite3.Row) -> EvaluationRecordRow:
    return EvaluationRecordRow(
        evaluation_id=row["evaluation_id"],
        record_id=row["record_id"],
        record_key=row["record_key"],
        gold_label=row["gold_label"],
        agent_label=row["agent_label"],
        agree=None if row["agree"] is None else int(row["agree"]),
        review_required=(
            None if row["review_required"] is None else int(row["review_required"])
        ),
        record_status=row["record_status"],
        failure_stage=row["failure_stage"],
    )


def insert_evaluation(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    split: str,
    gold: Mapping[str, Any],
    manifest: Mapping[str, Any],
    split_info: Mapping[str, Any],
    versions: Mapping[str, Any],
    metrics: Mapping[str, Any],
    thresholds: Sequence[Mapping[str, Any]],
    report_json_name: str,
    report_text_name: str,
    valid_count: int,
    scored_count: int,
    excluded_count: int,
    records: Sequence[Mapping[str, Any]],
    evaluation_id: str | None = None,
    created_at: str | None = None,
) -> EvaluationRow:
    """在一个事务里写入一次评估与它的逐条对照，返回写入行。

    调用方负责事务边界（`storage.write_transaction`）：汇总行与逐条行必须一起提交，
    否则页面会看到一个「有几条对照缺失」的评估，而那和「这几条没预测」是两回事。
    """
    identifier = evaluation_id or new_evaluation_id()
    stamp = created_at or utc_now()

    connection.execute(
        "INSERT INTO evaluations (evaluation_id, run_id, split, gold_filename, gold_sha256,"
        " gold_data_version, gold_contract_version, manifest_filename, manifest_sha256,"
        " manifest_version, seed, split_json, versions_json, metrics_json, thresholds_json,"
        " report_json_name, report_text_name, valid_count, scored_count, excluded_count,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            identifier,
            run_id,
            split,
            gold["filename"],
            gold["sha256"],
            gold["data_version"],
            gold["contract_version"],
            manifest["filename"],
            manifest["sha256"],
            manifest["version"],
            int(manifest["seed"]),
            json.dumps(split_info, ensure_ascii=False),
            json.dumps(versions, ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            json.dumps(list(thresholds), ensure_ascii=False),
            report_json_name,
            report_text_name,
            int(valid_count),
            int(scored_count),
            int(excluded_count),
            stamp,
        ),
    )

    connection.executemany(
        "INSERT INTO evaluation_records (evaluation_id, record_id, record_key, gold_label,"
        " agent_label, agree, review_required, record_status, failure_stage)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                identifier,
                item["record_id"],
                item.get("record_key"),
                item["gold_label"],
                item.get("agent_label"),
                None if item.get("agree") is None else int(bool(item["agree"])),
                None
                if item.get("review_required") is None
                else int(bool(item["review_required"])),
                item["record_status"],
                item.get("failure_stage"),
            )
            for item in records
        ],
    )

    row = connection.execute(
        "SELECT * FROM evaluations WHERE evaluation_id = ?", (identifier,)
    ).fetchone()
    return _row_to_evaluation(row)


def get_evaluation(
    connection: sqlite3.Connection, evaluation_id: str
) -> EvaluationRow | None:
    row = connection.execute(
        "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
    ).fetchone()
    return None if row is None else _row_to_evaluation(row)


#: 评估历史的排序：新→旧，同一时刻的用 rowid 兜底。
#:
#: **不能拿 evaluation_id 当兜底**：它是 uuid4，与先后无关，用它排序会让「同一秒内
#: 连评两次」的显示顺序随机——界面上的「最近一次评估」就可能不是最近那次。rowid 是
#: SQLite 的插入序号，单调递增，正好表达「谁后写的」。
_EVALUATION_ORDER = " ORDER BY created_at DESC, rowid DESC"


def list_evaluations(
    connection: sqlite3.Connection, run_id: str
) -> tuple[EvaluationRow, ...]:
    """某批次的评估历史，新→旧。"""
    return tuple(
        _row_to_evaluation(row)
        for row in connection.execute(
            "SELECT * FROM evaluations WHERE run_id = ?" + _EVALUATION_ORDER,
            (run_id,),
        )
    )


def list_evaluations_page(
    connection: sqlite3.Connection, run_id: str, *, limit: int, offset: int
) -> tuple[tuple[EvaluationRow, ...], int]:
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM evaluations WHERE run_id = ?", (run_id,)
    ).fetchone()
    rows = connection.execute(
        "SELECT * FROM evaluations WHERE run_id = ?"
        + _EVALUATION_ORDER
        + " LIMIT ? OFFSET ?",
        (run_id, int(limit), int(offset)),
    ).fetchall()
    return tuple(_row_to_evaluation(row) for row in rows), int(total["n"])


def list_evaluation_records(
    connection: sqlite3.Connection,
    evaluation_id: str,
    *,
    limit: int,
    offset: int,
    agree: bool | None = None,
    record_status: str | None = None,
    record_id: str | None = None,
) -> tuple[tuple[EvaluationRecordRow, ...], int]:
    """逐条对照的分页查询；排序固定按**业务编号**，与报告里的顺序一致。

    ``agree`` 传 ``False`` 只会命中「有预测但不一致」的行：没有预测的行 ``agree``
    是 NULL（技术失败/未跑完），它们要单独看，混进「不一致」里会把两种问题并成一种。
    """
    clauses = ["evaluation_id = ?"]
    params: list[Any] = [evaluation_id]
    if agree is not None:
        clauses.append("agree = ?")
        params.append(1 if agree else 0)
    if record_status is not None:
        clauses.append("record_status = ?")
        params.append(record_status)
    if record_id is not None:
        clauses.append("record_id = ?")
        params.append(record_id)

    where = " WHERE " + " AND ".join(clauses)
    total = connection.execute(
        f"SELECT COUNT(*) AS n FROM evaluation_records{where}", tuple(params)
    ).fetchone()
    rows = connection.execute(
        f"SELECT * FROM evaluation_records{where}"
        " ORDER BY CAST(record_id AS INTEGER), record_id LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    ).fetchall()
    return tuple(_row_to_evaluation_record(row) for row in rows), int(total["n"])


__all__ = [
    "EvaluationRecordRow",
    "EvaluationRow",
    "get_evaluation",
    "insert_evaluation",
    "list_evaluation_records",
    "list_evaluations",
    "list_evaluations_page",
    "new_evaluation_id",
]
