"""上传、预检与批次的读写（S04-02）。

批次（`runs`）直接引用它的来源——上传（`uploads`）与预检（`input_validations`），
所以这三张表放在同一个模块里，避免为了一个 join 在模块间绕圈。

**只读写行**：`POST /api/runs` 的业务判断（预检是否 passed、文件是否变过）在
`services/batches.py`。所有写操作走短事务，不在事务里做模型请求或文件生成。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schemas.qa import PrecheckReport

SheetEntry = tuple[str, bool]


@dataclass(frozen=True)
class UploadRow:
    """`uploads` 的一行。``relative_path`` 相对上传目录，路径由后端生成。"""

    upload_id: str
    original_filename: str
    relative_path: str
    sha256: str
    size_bytes: int
    sheets: tuple[SheetEntry, ...]
    created_at: str

    def path(self, uploads_dir: Path) -> Path:
        return Path(uploads_dir) / self.relative_path


@dataclass(frozen=True)
class ValidationRow:
    """`input_validations` 的一行；``report`` 是解析后的预检快照。"""

    validation_id: str
    upload_id: str
    sheet_name: str | None
    status: str
    file_sha256: str
    input_contract_version: str
    counts: dict[str, Any]
    report: PrecheckReport
    created_at: str


@dataclass(frozen=True)
class RunRow:
    """`runs` 的一行；动态计数（classified/failed 等）不在这里，聚合自 records。"""

    run_id: str
    upload_id: str
    validation_id: str
    source_filename: str
    sheet_name: str
    input_digest: str
    config_snapshot: dict[str, Any]
    versions: dict[str, Any]
    status: str
    revision: int
    total_count: int
    valid_count: int
    input_invalid_count: int
    skipped_blank_rows: int
    last_error: dict[str, Any] | None
    created_at: str
    started_at: str | None
    finished_at: str | None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _row_to_run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        run_id=row["run_id"],
        upload_id=row["upload_id"],
        validation_id=row["validation_id"],
        source_filename=row["source_filename"],
        sheet_name=row["sheet_name"],
        input_digest=row["input_digest"],
        config_snapshot=json.loads(row["config_snapshot_json"]),
        versions=json.loads(row["versions_json"]),
        status=row["status"],
        revision=int(row["revision"]),
        total_count=int(row["total_count"]),
        valid_count=int(row["valid_count"]),
        input_invalid_count=int(row["input_invalid_count"]),
        skipped_blank_rows=int(row["skipped_blank_rows"]),
        last_error=json.loads(row["last_error_json"]) if row["last_error_json"] else None,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


# ------------------------------------------------------------------ uploads


def insert_upload(
    connection: sqlite3.Connection,
    *,
    upload_id: str,
    original_filename: str,
    relative_path: str,
    sha256: str,
    size_bytes: int,
    sheets: tuple[SheetEntry, ...],
    created_at: str,
) -> None:
    """登记一次上传；调用方保证文件**已完整写入**。"""
    connection.execute(
        "INSERT INTO uploads (upload_id, original_filename, relative_path, sha256,"
        " size_bytes, sheets_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            upload_id,
            original_filename,
            relative_path,
            sha256,
            int(size_bytes),
            _json([[name, bool(visible)] for name, visible in sheets]),
            created_at,
        ),
    )


def get_upload(connection: sqlite3.Connection, upload_id: str) -> UploadRow | None:
    row = connection.execute(
        "SELECT * FROM uploads WHERE upload_id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        return None
    raw_sheets = json.loads(row["sheets_json"])
    return UploadRow(
        upload_id=row["upload_id"],
        original_filename=row["original_filename"],
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        size_bytes=int(row["size_bytes"]),
        sheets=tuple((str(name), bool(visible)) for name, visible in raw_sheets),
        created_at=row["created_at"],
    )


# ------------------------------------------------------- input_validations


def insert_input_validation(
    connection: sqlite3.Connection,
    *,
    validation_id: str,
    upload_id: str,
    report: PrecheckReport,
    created_at: str,
) -> None:
    """保存一次预检的不可变快照；**原预检不覆盖**（plan/09 §3）。"""
    connection.execute(
        "INSERT INTO input_validations (validation_id, upload_id, sheet_name, status,"
        " file_sha256, input_contract_version, counts_json, report_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            validation_id,
            upload_id,
            report.sheet_name,
            report.status,
            report.file_sha256,
            report.input_contract_version,
            _json(report.counts.model_dump(mode="json")),
            report.model_dump_json(),
            created_at,
        ),
    )


def get_input_validation(
    connection: sqlite3.Connection, validation_id: str
) -> ValidationRow | None:
    row = connection.execute(
        "SELECT * FROM input_validations WHERE validation_id = ?", (validation_id,)
    ).fetchone()
    if row is None:
        return None
    return ValidationRow(
        validation_id=row["validation_id"],
        upload_id=row["upload_id"],
        sheet_name=row["sheet_name"],
        status=row["status"],
        file_sha256=row["file_sha256"],
        input_contract_version=row["input_contract_version"],
        counts=json.loads(row["counts_json"]),
        report=PrecheckReport.model_validate_json(row["report_json"]),
        created_at=row["created_at"],
    )


# ------------------------------------------------------------------ runs


def insert_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    upload_id: str,
    validation_id: str,
    source_filename: str,
    sheet_name: str,
    input_digest: str,
    config_snapshot: dict[str, Any],
    prompt_snapshot: dict[str, Any],
    versions: dict[str, Any],
    report: PrecheckReport,
    status: str,
    created_at: str,
) -> None:
    """建立批次；计数在创建时固定，之后只随记录状态变化更新 revision。"""
    counts = report.counts
    connection.execute(
        "INSERT INTO runs (run_id, upload_id, validation_id, source_filename, sheet_name,"
        " input_digest, config_snapshot_json, prompt_snapshot_json, versions_json, status,"
        " revision, total_count, valid_count, input_invalid_count, skipped_blank_rows,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
        (
            run_id,
            upload_id,
            validation_id,
            source_filename,
            sheet_name,
            input_digest,
            _json(config_snapshot),
            _json(prompt_snapshot),
            _json(versions),
            status,
            int(counts.total or 0),
            int(counts.valid or 0),
            int(counts.input_invalid or 0),
            int(counts.skipped_blank_rows or 0),
            created_at,
        ),
    )


def get_run(connection: sqlite3.Connection, run_id: str) -> RunRow | None:
    row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else _row_to_run(row)


def list_runs(
    connection: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    status: str | None = None,
) -> tuple[tuple[RunRow, ...], int]:
    """批次列表与**筛选后的**总数；排序固定 ``created_at DESC, run_id DESC``。

    [plan/08 §5] 的排序键是「created_at、run_id 倒序」：不加 run_id 兜底时，同一秒
    创建的批次在翻页时顺序不定，会出现漏项或重复。筛选只接受合法批次状态（由
    `services/batches.py` 校验，非法值在到达这里之前已被拒）。
    """
    where = "" if status is None else " WHERE status = ?"
    params: tuple[Any, ...] = () if status is None else (status,)
    total = connection.execute(f"SELECT COUNT(*) AS n FROM runs{where}", params).fetchone()
    rows = connection.execute(
        f"SELECT * FROM runs{where} ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    ).fetchall()
    return tuple(_row_to_run(row) for row in rows), int(total["n"])


def bump_revision(connection: sqlite3.Connection, run_id: str) -> int:
    """批次可见状态变化时递增 revision；返回新值。

    与触发它的记录/批次更新**同一事务**（[plan/09 §…“保存阶段”]）；导出按
    `scheduled_revision` 去重，靠的就是这个单调值。
    """
    connection.execute(
        "UPDATE runs SET revision = revision + 1 WHERE run_id = ?", (run_id,)
    )
    row = connection.execute(
        "SELECT revision FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"未知 run_id：{run_id}")
    return int(row["revision"])


def finish_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    finished_at: str,
    last_error: dict[str, Any] | None = None,
) -> None:
    """批次进入终态；与任务的终态更新**同一事务**（[plan/10 §2]）。

    `last_error_json` 只在有失败行时写入：没有失败就把它清空，避免上一轮的
    错误残留在已经恢复成功的批次上。
    """
    connection.execute(
        "UPDATE runs SET status = ?, finished_at = ?, last_error_json = ? WHERE run_id = ?",
        (
            status,
            finished_at,
            _json(last_error) if last_error is not None else None,
            run_id,
        ),
    )


def reopen_run(connection: sqlite3.Connection, *, run_id: str) -> None:
    """恢复入队时把批次改回 **queued**（[plan/08 §5]）。

    只改三件事：状态、`finished_at` **清空**、`revision` 递增。`created_at` 与
    `started_at` 保持不变——前者是来源留痕，后者是「第一次开始」的事实；操作历史
    由任务表承载，不在批次行上做时间线。`last_error_json` 也**保留**：它是上一次
    终态的失败摘要，新的终态由 `finish_run` 覆写或清空。
    """
    connection.execute(
        "UPDATE runs SET status = 'queued', finished_at = NULL,"
        " revision = revision + 1 WHERE run_id = ?",
        (run_id,),
    )


def get_prompt_snapshot(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """批次创建时冻结的提示词快照；恢复时**必须**用它而不是当前磁盘内容。"""
    row = connection.execute(
        "SELECT prompt_snapshot_json FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"未知 run_id：{run_id}")
    return dict(json.loads(row["prompt_snapshot_json"]))


__all__ = [
    "RunRow",
    "SheetEntry",
    "UploadRow",
    "ValidationRow",
    "bump_revision",
    "finish_run",
    "get_input_validation",
    "get_prompt_snapshot",
    "get_run",
    "get_upload",
    "insert_input_validation",
    "insert_run",
    "insert_upload",
    "list_runs",
    "reopen_run",
]
