"""导出与已登记文件的读写（S05-03）。

**只读写行**：文件生成、发布顺序与失败处理在 `services/exports.py`，导出任务的
状态机在 `worker.py`。这一层不碰磁盘。

`exports` 与 `artifacts` 两张表在 S04 迁移里就已建好（[001_init.sql]），本模块是
第一批真正写它的代码。三条约束由数据库兜底，服务层不重复维护：

- `exports.job_id` 唯一：一份导出对应且只对应一个任务；
- `uq_automatic_export_revision`：同一批次同一 revision 只自动安排一次；
- `artifacts(export_id, kind)` 唯一：一份导出里 Excel 与 JSONL 各一份。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any


def new_export_id() -> str:
    return uuid.uuid4().hex


def new_artifact_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class ExportRow:
    """`exports` 的一行；``captured_*`` 在 worker 认领时才填（[plan/10 §8]）。"""

    export_id: str
    job_id: str
    run_id: str
    source: str
    scheduled_revision: int
    captured_revision: int | None
    captured_at: str | None
    captured_run_status: str | None
    captured_counts: dict[str, Any] | None
    created_at: str

    @property
    def captured(self) -> bool:
        """快照是否已捕获；未捕获时界面显示「尚未捕获快照」。"""
        return self.captured_at is not None


@dataclass(frozen=True)
class ArtifactRow:
    """`artifacts` 的一行；只有**已完整发布**的文件才会出现在这里。"""

    artifact_id: str
    export_id: str
    kind: str
    relative_path: str
    download_name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: str


def _row_to_export(row: sqlite3.Row) -> ExportRow:
    return ExportRow(
        export_id=row["export_id"],
        job_id=row["job_id"],
        run_id=row["run_id"],
        source=row["source"],
        scheduled_revision=int(row["scheduled_revision"]),
        captured_revision=(
            int(row["captured_revision"])
            if row["captured_revision"] is not None
            else None
        ),
        captured_at=row["captured_at"],
        captured_run_status=row["captured_run_status"],
        captured_counts=(
            json.loads(row["captured_counts_json"])
            if row["captured_counts_json"]
            else None
        ),
        created_at=row["created_at"],
    )


def _row_to_artifact(row: sqlite3.Row) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=row["artifact_id"],
        export_id=row["export_id"],
        kind=row["kind"],
        relative_path=row["relative_path"],
        download_name=row["download_name"],
        media_type=row["media_type"],
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
        created_at=row["created_at"],
    )


# ------------------------------------------------------------------ exports


def insert_export(
    connection: sqlite3.Connection,
    *,
    export_id: str,
    job_id: str,
    run_id: str,
    source: str,
    scheduled_revision: int,
    created_at: str,
) -> None:
    """登记一份导出；与它的 export job **同一事务**。

    自动导出撞上 `uq_automatic_export_revision` 时抛 ``IntegrityError``：同一
    revision 已经排过一次，调用方按「去重命中」处理，不再排第二个任务。
    """
    connection.execute(
        "INSERT INTO exports (export_id, job_id, run_id, source, scheduled_revision,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (export_id, job_id, run_id, source, int(scheduled_revision), created_at),
    )


def mark_captured(
    connection: sqlite3.Connection,
    *,
    export_id: str,
    captured_revision: int,
    captured_at: str,
    captured_run_status: str,
    captured_counts: dict[str, Any],
) -> None:
    """记录快照的捕获时点；**只在 worker 认领导出任务时**调用一次。"""
    connection.execute(
        "UPDATE exports SET captured_revision = ?, captured_at = ?,"
        " captured_run_status = ?, captured_counts_json = ? WHERE export_id = ?",
        (
            int(captured_revision),
            captured_at,
            captured_run_status,
            json.dumps(captured_counts, ensure_ascii=False),
            export_id,
        ),
    )


def get_export(connection: sqlite3.Connection, export_id: str) -> ExportRow | None:
    row = connection.execute(
        "SELECT * FROM exports WHERE export_id = ?", (export_id,)
    ).fetchone()
    return None if row is None else _row_to_export(row)


def get_export_by_job(connection: sqlite3.Connection, job_id: str) -> ExportRow | None:
    row = connection.execute(
        "SELECT * FROM exports WHERE job_id = ?", (job_id,)
    ).fetchone()
    return None if row is None else _row_to_export(row)


#: 导出历史与「最近一次导出」共用的排序：时间倒序，同秒按插入顺序倒序。
#: `created_at` 只到秒，同一秒内的两次导出再按 `export_id`（uuid）排就是随机顺序
#: ——「最近一次导出」必须和列表第一行永远是同一个，所以两处用同一条排序。
_RECENT_FIRST = "ORDER BY created_at DESC, rowid DESC"


def list_exports(
    connection: sqlite3.Connection, run_id: str, *, limit: int, offset: int
) -> tuple[tuple[ExportRow, ...], int]:
    """导出历史；新→旧（与 `latest_export` 同一条排序）。"""
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM exports WHERE run_id = ?", (run_id,)
    ).fetchone()
    rows = connection.execute(
        f"SELECT * FROM exports WHERE run_id = ? {_RECENT_FIRST} LIMIT ? OFFSET ?",
        (run_id, int(limit), int(offset)),
    ).fetchall()
    return tuple(_row_to_export(row) for row in rows), int(total["n"])


def latest_export(connection: sqlite3.Connection, run_id: str) -> ExportRow | None:
    """最近一次导出；没有时 ``None``（`run_detail` 的 `latest_export`）。"""
    row = connection.execute(
        f"SELECT * FROM exports WHERE run_id = ? {_RECENT_FIRST} LIMIT 1",
        (run_id,),
    ).fetchone()
    return None if row is None else _row_to_export(row)


def find_export_by_revision(
    connection: sqlite3.Connection, *, run_id: str, scheduled_revision: int
) -> ExportRow | None:
    """该批次在该 revision 上已经安排的**自动**导出；用于去重核对。"""
    row = connection.execute(
        "SELECT * FROM exports WHERE run_id = ? AND scheduled_revision = ?"
        " AND source = 'automatic'",
        (run_id, int(scheduled_revision)),
    ).fetchone()
    return None if row is None else _row_to_export(row)


# ---------------------------------------------------------------- artifacts


def insert_artifact(
    connection: sqlite3.Connection,
    *,
    artifact_id: str,
    export_id: str,
    kind: str,
    relative_path: str,
    download_name: str,
    media_type: str,
    size_bytes: int,
    sha256: str,
    created_at: str,
) -> None:
    """登记一份**已完整落盘**的文件；两份齐全才把任务置 completed。"""
    connection.execute(
        "INSERT INTO artifacts (artifact_id, export_id, kind, relative_path,"
        " download_name, media_type, size_bytes, sha256, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            export_id,
            kind,
            relative_path,
            download_name,
            media_type,
            int(size_bytes),
            sha256,
            created_at,
        ),
    )


def list_artifacts(
    connection: sqlite3.Connection, export_id: str
) -> tuple[ArtifactRow, ...]:
    """一份导出的全部文件；按 `kind` 排序，`excel` 在前、`jsonl` 在后。"""
    return tuple(
        _row_to_artifact(row)
        for row in connection.execute(
            "SELECT * FROM artifacts WHERE export_id = ? ORDER BY kind", (export_id,)
        )
    )


def get_artifact(
    connection: sqlite3.Connection, artifact_id: str
) -> ArtifactRow | None:
    row = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    return None if row is None else _row_to_artifact(row)


__all__ = [
    "ArtifactRow",
    "ExportRow",
    "find_export_by_revision",
    "get_artifact",
    "get_export",
    "get_export_by_job",
    "insert_artifact",
    "insert_export",
    "latest_export",
    "list_artifacts",
    "list_exports",
    "mark_captured",
    "new_artifact_id",
    "new_export_id",
]
