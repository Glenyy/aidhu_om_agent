"""S05-02、S05-03：导出快照、两份产物装配与成对发布。

分三段，对应两个小步骤：

- **装载**（`load_snapshot`）：在**一个读事务**里把该批次的全部记录、阶段结果与
  调用尝试读进内存。调用方负责开事务，读完就结束——排队期间新产生的分类结果
  不会挤进这份导出（[plan/10 §8]）。
- **装配**（`build_document`）：纯函数，内存快照 → 四表行 + 两阶段 JSONL 字节。
  原始资料改动全在这里发生（截断、公式前缀、控制字符清理），并逐项记账。
- **写出与发布**（`write_document`、`publish_document`）：先写临时文件，两份都好
  再改名，最后**一个写事务**登记两条 artifacts 并把任务置 completed。

生成的顺序是被约束的：JSONL 先写、算出摘要，概况表才能把它的文件名与 sha256
写进 Excel；Excel 自己的摘要只能落在 artifacts 表里（文件放不进自己的摘要）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..excel.writer import CellValue, SheetData, write_workbook
from ..repositories import exports as exports_repo
from ..repositories import jobs as jobs_repo
from ..repositories import records as records_repo
from ..repositories import runs as runs_repo
from ..schemas.export import (
    ARTIFACT_EXCEL,
    ARTIFACT_JSONL,
    ARTIFACT_KINDS,
    ARTIFACT_MEDIA_TYPES,
    CLASSIFICATION_COLUMNS,
    EXCEL_DIGEST_NOTE,
    EXPORT_CONTRACT_VERSION,
    FAILURE_COLUMNS,
    REVIEW_COLUMNS,
    SHEET_CLASSIFICATION,
    SHEET_FAILURES,
    SHEET_REVIEW,
    SHEET_SUMMARY,
    SOURCE_AUTOMATIC,
    SOURCE_MANUAL,
    SUMMARY_COLUMNS,
    SUMMARY_FIELDS,
    UNPROCESSED_FIELD,
    cell_location,
    download_name,
    excel_cell_text,
    format_evidence,
    ordinal,
    record_status_text,
    retryable_text,
    review_required_text,
    run_status_text,
    sanitized_cell_location,
    stage_text,
)
from ..schemas.qa import REF_FIELDS
from ..storage import (
    Database,
    DatabaseBusyError,
    read_transaction,
    utc_now,
    write_transaction,
)
from .batches import BatchError, counts_of

__all__ = [
    "ArtifactDownload",
    "ExportDocument",
    "ExportOutcome",
    "ExportSnapshot",
    "ExportStateError",
    "ManualExport",
    "RecordSnapshot",
    "TEMP_SUFFIX",
    "WrittenArtifact",
    "artifact_download",
    "build_document",
    "create_manual_export",
    "execute_export",
    "export_directory",
    "export_scope",
    "export_view",
    "latest_export_view",
    "list_export_views",
    "load_snapshot",
    "publish_export",
    "schedule_automatic_export",
    "write_document",
]


class ExportStateError(RuntimeError):
    """快照与记录状态不自洽（如 completed 却没有阶段二结果）。停止并报告。"""


# --------------------------------------------------------------------------
# 内存快照（S05-02）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordSnapshot:
    """一条记录的完整导出素材：记录行 + 两个阶段结果 + 全部调用尝试。"""

    record: records_repo.RecordRow
    stage1: records_repo.StageResultRow | None
    stage2: records_repo.StageResultRow | None
    attempts_by_stage: dict[int, tuple[records_repo.AttemptRow, ...]]

    @property
    def attempts(self) -> tuple[records_repo.AttemptRow, ...]:
        """全部阶段的尝试，按阶段顺序拼接。"""
        return tuple(
            attempt
            for stage in sorted(self.attempts_by_stage)
            for attempt in self.attempts_by_stage[stage]
        )

    @property
    def number(self) -> int:
        """人读序号 1..N；由 ``order_index + 1`` 得来，不动存储值。"""
        return ordinal(self.record.order_index)

    def stage_payload(self, stage: int) -> dict[str, Any] | None:
        """已校验的阶段结果正文；没结果时为 ``None``。

        直接写回 JSONL 的**原文**（`result_json` 解析后的对象），不重新序列化
        成模型：阶段二里的 `stage1_corrections` 等字段必须原样保留。
        """
        row = self.stage1 if stage == 1 else self.stage2
        if row is None:
            return None
        return json.loads(row.result_json)

    @property
    def stage2_payload(self) -> dict[str, Any] | None:
        return self.stage_payload(2)

    @property
    def label(self) -> str | None:
        """最终三分类；未完成或失败时为 ``None``（不填造标签）。"""
        return self.record.final_label

    @property
    def reason(self) -> str | None:
        payload = self.stage2_payload
        if payload is None:
            return None
        value = payload.get("reason")
        return None if value is None else str(value)

    @property
    def evidence(self) -> list[dict[str, Any]]:
        payload = self.stage2_payload
        if payload is None:
            return []
        raw = payload.get("evidence") or []
        return [dict(item) for item in raw if isinstance(item, dict)]

    @property
    def review_reasons(self) -> list[str]:
        payload = self.stage2_payload
        if payload is None:
            return []
        return [str(item) for item in (payload.get("review_reasons") or [])]

    def attempt_summary(self) -> dict[str, Any]:
        """按阶段的尝试计数（含历史轮次）；只报事实，不含模型输出。

        `unknown_after_interrupt` 也计入 `total`：它已经占了预算（[plan/09 §4]），
        不把它算进去会让「尝试次数」与实际花掉的次数对不上。
        """
        summary: dict[str, Any] = {
            "total": 0,
            "stage1": 0,
            "stage2": 0,
            "failed": 0,
            "unknown_after_interrupt": 0,
            "simulated": 0,
        }
        for stage, attempts in self.attempts_by_stage.items():
            key = f"stage{stage}"
            if key in summary:
                summary[key] = len(attempts)
            summary["total"] += len(attempts)
            for attempt in attempts:
                if attempt.status == records_repo.ATTEMPT_FAILED:
                    summary["failed"] += 1
                elif attempt.status == records_repo.ATTEMPT_UNKNOWN:
                    summary["unknown_after_interrupt"] += 1
                if attempt.simulated:
                    summary["simulated"] += 1
        return summary


@dataclass(frozen=True)
class ExportSnapshot:
    """一次导出在**认领那一刻**看到的批次全貌。"""

    run: runs_repo.RunRow
    records: tuple[RecordSnapshot, ...]
    counts: dict[str, int]
    captured_at: str

    @property
    def unprocessed(self) -> int:
        """捕获时仍未处理的记录数；不为 0 就不能把这份导出当完整批次。"""
        return int(self.counts.get("remaining", 0))


def load_snapshot(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    captured_at: str,
    counts: dict[str, int],
) -> ExportSnapshot:
    """在调用方的读事务里装载快照。

    ``counts`` 由调用方用 `services.batches.counts_of` 在**同一事务**里算出，
    这样概况表的计数与界面显示的是同一个公式、同一份快照。
    """
    run = runs_repo.get_run(connection, run_id)
    if run is None:
        raise ExportStateError(f"未知 run_id：{run_id}")

    records: list[RecordSnapshot] = []
    for record in records_repo.list_records(connection, run_id):
        records.append(
            RecordSnapshot(
                record=record,
                stage1=records_repo.get_stage_result(connection, record.record_key, 1),
                stage2=records_repo.get_stage_result(connection, record.record_key, 2),
                attempts_by_stage=records_repo.list_attempts_by_stage(
                    connection, record.record_key
                ),
            )
        )
    return ExportSnapshot(
        run=run,
        records=tuple(records),
        counts=dict(counts),
        captured_at=captured_at,
    )


# --------------------------------------------------------------------------
# 装配（S05-02）
# --------------------------------------------------------------------------


class _CellTracker:
    """记录每处「为了落进 Excel 而改写过原文」的单元格。

    改写本身有账可查：概况表给出计数与位置，原文在 JSONL 里一条不少。
    """

    def __init__(self) -> None:
        self.truncated: list[str] = []
        self.sanitized: list[str] = []
        self.guarded = 0

    def text(
        self,
        sheet: str,
        column: str,
        row: int,
        value: str | None,
    ) -> str:
        cell = excel_cell_text(value)
        if cell.truncated:
            self.truncated.append(cell_location(sheet, column, row, len(value or "")))
        if cell.sanitized:
            self.sanitized.append(
                sanitized_cell_location(sheet, column, row, cell.sanitized)
            )
        if cell.guarded:
            self.guarded += 1
        return cell.text


@dataclass(frozen=True)
class DocumentStats:
    """装配过程的可核对计数；概况表与单测都用它。"""

    record_count: int
    review_count: int
    failure_count: int
    guarded_cells: int
    truncated_cells: tuple[str, ...]
    sanitized_cells: tuple[str, ...]


@dataclass(frozen=True)
class ExportDocument:
    """装配完成、还没落盘的产物：JSONL 字节 + 四张表的行。"""

    export_id: str
    run_id: str
    jsonl_bytes: bytes
    jsonl_sha256: str
    sheets: tuple[SheetData, ...]
    stats: DocumentStats


def _classification_rows(
    snapshot: ExportSnapshot, tracker: _CellTracker
) -> tuple[tuple[CellValue, ...], ...]:
    """分类结果表：**每条记录一行**（含输入失败行），顺序即 order_index。"""
    sheet = SHEET_CLASSIFICATION
    rows: list[tuple[CellValue, ...]] = []
    for item in snapshot.records:
        record = item.record
        number = item.number
        row = number + 1  # 表头占第 1 行；位置记账用物理行号
        rows.append(
            (
                number,
                tracker.text(sheet, "原始编号", row, record.record_id),
                record.source_row,
                tracker.text(sheet, "原预测", row, item.label),
                tracker.text(sheet, "理由", row, item.reason),
                tracker.text(
                    sheet, "证据", row, format_evidence(item.evidence) or None
                ),
                review_required_text(record.review_required),
                record_status_text(record.status),
            )
        )
    return tuple(rows)


def _review_rows(
    snapshot: ExportSnapshot, tracker: _CellTracker
) -> tuple[tuple[CellValue, ...], ...]:
    """复核清单：只放被标记的记录；两列人工填写列导出时留空。"""
    sheet = SHEET_REVIEW
    rows: list[tuple[CellValue, ...]] = []
    for item in snapshot.records:
        record = item.record
        if not record.review_required:
            continue
        number = item.number
        # 复核清单只放被标记的记录，位置要按**本表**的落盘顺序数，不能沿用整批序号。
        row = len(rows) + 2
        refs = [record.refs.get(ref_id) for ref_id in REF_FIELDS]
        rows.append(
            (
                number,
                tracker.text(sheet, "原始编号", row, record.record_id),
                record.source_row,
                tracker.text(sheet, "q", row, record.q),
                tracker.text(sheet, "a", row, record.a),
                *(
                    tracker.text(sheet, ref_id, row, refs[index])
                    for index, ref_id in enumerate(REF_FIELDS)
                ),
                tracker.text(sheet, "原预测", row, item.label),
                tracker.text(sheet, "理由", row, item.reason),
                tracker.text(
                    sheet, "证据", row, format_evidence(item.evidence) or None
                ),
                review_required_text(record.review_required),
                "",  # 人工判断：留给人工，导出不预填
                "",  # 复核说明：同上
            )
        )
    return tuple(rows)


def _failure_rows(
    snapshot: ExportSnapshot, tracker: _CellTracker
) -> tuple[tuple[CellValue, ...], ...]:
    """失败清单：只列有效记录的技术失败；输入失败在分类结果表里另标状态。"""
    sheet = SHEET_FAILURES
    rows: list[tuple[CellValue, ...]] = []
    for item in snapshot.records:
        record = item.record
        if record.status != "failed":
            continue
        failure = record.failure or {}
        number = item.number
        row = len(rows) + 2  # 只列技术失败，位置按本表落盘顺序
        rows.append(
            (
                number,
                tracker.text(sheet, "原始编号", row, record.record_id),
                record.source_row,
                stage_text(record.failure_stage),
                tracker.text(sheet, "错误码", row, _as_text(failure.get("code"))),
                tracker.text(
                    sheet, "错误信息", row, _as_text(failure.get("message"))
                ),
                tracker.text(
                    sheet, "尝试次数", row, _as_text(failure.get("attempt_count"))
                ),
                retryable_text(failure.get("retryable")),
            )
        )
    return tuple(rows)


def _as_text(value: object) -> str | None:
    """失败详情里的值转成文本；缺失留空，不写占位文字。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _summary_rows(
    snapshot: ExportSnapshot,
    tracker: _CellTracker,
    *,
    jsonl_name: str,
    jsonl_sha256: str,
) -> tuple[tuple[CellValue, ...], ...]:
    """运行概况：固定标签顺序的键值表，值全部可回库/回界面核对。"""
    sheet = SHEET_SUMMARY
    run = snapshot.run
    versions = run.versions
    counts = snapshot.counts
    values: dict[str, CellValue] = {
        "批次标识": run.run_id,
        "来源文件": run.source_filename,
        "工作表": run.sheet_name,
        "输入摘要": run.input_digest,
        "批次状态（捕获时）": run_status_text(run.status),
        "捕获时间": snapshot.captured_at,
        "捕获修订": run.revision,
        "总数": counts.get("total", 0),
        "有效记录": counts.get("valid", 0),
        "输入失败": counts.get("input_invalid", 0),
        "已分类": counts.get("classified", 0),
        "技术失败": counts.get("failed", 0),
        UNPROCESSED_FIELD: counts.get("remaining", 0),
        "需复核": counts.get("review_required", 0),
        "被截断单元格数": len(tracker.truncated),
        "被截断单元格位置": "\n".join(tracker.truncated),
        "文本前缀防护单元格数": tracker.guarded,
        "清理控制字符单元格数": len(tracker.sanitized),
        "清理控制字符位置": "\n".join(tracker.sanitized),
        "程序版本": _as_text(versions.get("program")),
        "提示词版本": (
            f"阶段一 {versions.get('stage1_prompt')}／阶段二 {versions.get('stage2_prompt')}"
        ),
        "阶段一 schema 版本": _as_text(versions.get("stage1_schema")),
        "阶段二 schema 版本": _as_text(versions.get("stage2_schema")),
        "输入合同版本": _as_text(versions.get("input_contract")),
        "导出合同版本": EXPORT_CONTRACT_VERSION,
        "JSONL 文件": jsonl_name,
        "JSONL 文件摘要": jsonl_sha256,
        "Excel 文件摘要": EXCEL_DIGEST_NOTE,
    }
    unknown = [field for field in SUMMARY_FIELDS if field not in values]
    if unknown:  # pragma: no cover - 常量与装配表写岔了才会到这里
        raise ExportStateError(f"概况表缺少这些行：{unknown}")

    rows: list[tuple[CellValue, ...]] = []
    for field in SUMMARY_FIELDS:
        row = len(rows) + 2  # 表头占第 1 行；位置记账用物理行号
        rows.append(
            (
                tracker.text(sheet, "项目", row, field),
                _summary_value(tracker, sheet, field, row, values[field]),
            )
        )
    return tuple(rows)


def _summary_value(
    tracker: _CellTracker, sheet: str, field: str, number: int, value: CellValue
) -> CellValue:
    if isinstance(value, int):
        return value
    return tracker.text(sheet, field, number, value)


def build_document(snapshot: ExportSnapshot, *, export_id: str) -> ExportDocument:
    """把内存快照装配成四张表与一份两阶段 JSONL（**不写盘**）。

    JSONL 先装配，因为概况表要写它的文件名与 sha256；两份产物用同一个
    ``export_id``，下载名里的短 id 由此而来。
    """
    jsonl_name = download_name(
        ARTIFACT_JSONL, run_id=snapshot.run.run_id, export_id=export_id
    )
    jsonl_bytes = _jsonl_bytes(snapshot, export_id=export_id)

    tracker = _CellTracker()
    classification = _classification_rows(snapshot, tracker)
    review = _review_rows(snapshot, tracker)
    failures = _failure_rows(snapshot, tracker)
    summary = _summary_rows(
        snapshot,
        tracker,
        jsonl_name=jsonl_name,
        jsonl_sha256=hashlib.sha256(jsonl_bytes).hexdigest(),
    )

    sheets = (
        SheetData(SHEET_CLASSIFICATION, CLASSIFICATION_COLUMNS, classification),
        SheetData(SHEET_REVIEW, REVIEW_COLUMNS, review),
        SheetData(SHEET_FAILURES, FAILURE_COLUMNS, failures),
        SheetData(SHEET_SUMMARY, SUMMARY_COLUMNS, summary),
    )
    return ExportDocument(
        export_id=export_id,
        run_id=snapshot.run.run_id,
        jsonl_bytes=jsonl_bytes,
        jsonl_sha256=hashlib.sha256(jsonl_bytes).hexdigest(),
        sheets=sheets,
        stats=DocumentStats(
            record_count=len(snapshot.records),
            review_count=len(review),
            failure_count=len(failures),
            guarded_cells=tracker.guarded,
            truncated_cells=tuple(tracker.truncated),
            sanitized_cells=tuple(tracker.sanitized),
        ),
    )


def _jsonl_bytes(snapshot: ExportSnapshot, *, export_id: str) -> bytes:
    """每条记录一行；**原文完整保留**，不做截断与清理。"""
    lines: list[str] = []
    for item in snapshot.records:
        record = item.record
        payload: dict[str, Any] = {
            "export_id": export_id,
            "run_id": snapshot.run.run_id,
            "ordinal": item.number,
            "record_key": record.record_key,
            "record_id": record.record_id,
            "source_row": record.source_row,
            "order_index": record.order_index,
            "q": record.q,
            "a": record.a,
            "refs": {ref_id: record.refs.get(ref_id) for ref_id in REF_FIELDS},
            "status": record.status,
            "label": item.label,
            "review_required": (
                None if record.review_required is None else bool(record.review_required)
            ),
            "reason": item.reason,
            "evidence": item.evidence,
            "review_reasons": item.review_reasons,
            "stage1": item.stage_payload(1),
            "stage2": item.stage2_payload,
            "failure": record.failure,
            "attempt_summary": item.attempt_summary(),
            "versions": {**snapshot.run.versions, "export_contract": EXPORT_CONTRACT_VERSION},
        }
        lines.append(json.dumps(payload, ensure_ascii=False))
    return ("\n".join(lines) + "\n" if lines else "").encode("utf-8")


# --------------------------------------------------------------------------
# 写出与成对发布（S05-03）
# --------------------------------------------------------------------------

#: 临时文件后缀。两份都用同一个后缀，改名成功前不会出现在 artifacts 里。
TEMP_SUFFIX = ".part"


@dataclass(frozen=True)
class WrittenArtifact:
    """一份**已经落盘**的产物；``relative_path`` 相对导出根目录（outputs）。"""

    kind: str
    path: Path
    relative_path: str
    download_name: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ExportOutcome:
    """一次导出执行的结局；供 worker、CLI 与测试核对，不进入接口合同。

    ``counts`` 是**捕获时**的批次计数，与写进概况表、`exports.captured_counts_json`
    的同一份值。
    """

    export_id: str
    run_id: str
    artifacts: tuple[WrittenArtifact, ...]
    stats: DocumentStats
    counts: dict[str, int]


def export_directory(outputs_root: Path, *, run_id: str, export_id: str) -> Path:
    """``outputs/<run_id>/<export_id>/``（[plan/05 §2]、[plan/09 §8]）。

    目录名只由程序生成的 id 组成：**原文件名不参与路径**，用户改一个文件名不会
    把导出写到别处。
    """
    return Path(outputs_root) / run_id / export_id


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def write_document(
    document: ExportDocument, *, outputs_root: Path
) -> tuple[WrittenArtifact, ...]:
    """写两份文件并改名到最终路径；**两份都好才算写出**。

    顺序按 [plan/09 §8]：先写 ``.part``，全部写好并关闭后才改名成最终文件。
    半途失败时删掉本次留下的 ``.part``（可证明是未完成的临时文件），**从不删
    已改名的最终文件**——那属于「文件写好但登记前中断」，按设计保留待人工处理。
    """
    directory = export_directory(
        outputs_root, run_id=document.run_id, export_id=document.export_id
    )
    directory.mkdir(parents=True, exist_ok=True)

    excel_name = download_name(
        ARTIFACT_EXCEL, run_id=document.run_id, export_id=document.export_id
    )
    staged: list[tuple[str, Path, Path]] = []  # (kind, temp, final)
    try:
        jsonl_final = directory / download_name(
            ARTIFACT_JSONL, run_id=document.run_id, export_id=document.export_id
        )
        jsonl_temp = jsonl_final.with_name(jsonl_final.name + TEMP_SUFFIX)
        jsonl_temp.write_bytes(document.jsonl_bytes)
        staged.append((ARTIFACT_JSONL, jsonl_temp, jsonl_final))

        excel_final = directory / excel_name
        excel_temp = excel_final.with_name(excel_final.name + TEMP_SUFFIX)
        write_workbook(excel_temp, document.sheets)
        staged.append((ARTIFACT_EXCEL, excel_temp, excel_final))
    except BaseException:
        for _, temp, _final in staged:
            temp.unlink(missing_ok=True)
        # Excel 可能在写盘过程中自己留下同名临时文件，一并清掉。
        for leftover in directory.glob(f"*{TEMP_SUFFIX}"):
            leftover.unlink(missing_ok=True)
        raise

    written: list[WrittenArtifact] = []
    for kind, temp, final in staged:
        # 同一目录内改名；Windows 上 os.replace 覆盖已存在文件是原子的。
        os.replace(temp, final)
        size, sha256 = _digest_file(final)
        written.append(
            WrittenArtifact(
                kind=kind,
                path=final,
                relative_path=final.relative_to(outputs_root).as_posix(),
                download_name=final.name,
                media_type=ARTIFACT_MEDIA_TYPES[kind],
                size_bytes=size,
                sha256=sha256,
            )
        )
    return tuple(sorted(written, key=lambda artifact: artifact.kind))


def publish_export(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    export_id: str,
    artifacts: Sequence[WrittenArtifact],
    created_at: str,
) -> tuple[exports_repo.ArtifactRow, ...]:
    """在一个写事务里登记两份文件并把导出任务置 ``completed``。

    **只有一份有效时不发布半组**：`artifacts` 必须同时含 `excel` 与 `jsonl`，
    否则抛错，让上层把任务标成失败。这里不接收半个结果，也不登记缺失的那份。
    """
    kinds = sorted(artifact.kind for artifact in artifacts)
    if kinds != sorted(ARTIFACT_KINDS):
        raise ExportStateError(
            f"导出 {export_id} 只有 {kinds}，缺 {'、'.join(ARTIFACT_KINDS)} 中的一份；"
            "不成对的文件不发布"
        )
    for artifact in artifacts:
        exports_repo.insert_artifact(
            connection,
            artifact_id=exports_repo.new_artifact_id(),
            export_id=export_id,
            kind=artifact.kind,
            relative_path=artifact.relative_path,
            download_name=artifact.download_name,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            created_at=created_at,
        )
    jobs_repo.finish_job(
        connection,
        job_id=job_id,
        status=jobs_repo.JOB_COMPLETED,
        finished_at=created_at,
        result={
            "export_id": export_id,
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "download_name": artifact.download_name,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                }
                for artifact in artifacts
            ],
        },
    )
    return exports_repo.list_artifacts(connection, export_id)


def schedule_automatic_export(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    revision: int,
    created_at: str,
) -> str | None:
    """在**判别终态那一个事务里**安排一次自动导出；已排过则返回 ``None``。

    与 `finish_run`/`finish_job` 同一个写事务，所以界面看到终态的那一刻，导出
    任务已经存在——不存在「刚变成完成，导出还没排上」的中间态。

    去重靠 `uq_automatic_export_revision`（同一批次同一 revision 只自动排一次）。
    这里先在同一事务内查一次而不是直接撞唯一索引：`jobs` 行要先插入（`exports.job_id`
    是外键），撞索引会留下一条没有 `exports` 行的孤儿任务。
    """
    existing = exports_repo.find_export_by_revision(
        connection, run_id=run_id, scheduled_revision=revision
    )
    if existing is not None:
        return None

    job_id = jobs_repo.new_job_id()
    jobs_repo.insert_job(
        connection,
        job_id=job_id,
        run_id=run_id,
        kind=jobs_repo.EXPORT_KIND,
        mode=jobs_repo.EXPORT_MODE_AUTOMATIC,
        payload={},
        created_at=created_at,
    )
    export_id = exports_repo.new_export_id()
    exports_repo.insert_export(
        connection,
        export_id=export_id,
        job_id=job_id,
        run_id=run_id,
        source=SOURCE_AUTOMATIC,
        scheduled_revision=revision,
        created_at=created_at,
    )
    return export_id


def execute_export(
    database,
    *,
    job: jobs_repo.JobRow,
    outputs_root: Path,
    captured_at: str | None = None,
) -> ExportOutcome:
    """执行一个导出任务：捕获快照 → 生成文件 → 成对登记。

    快照捕获用的是**一个读事务**，事务结束才开始写文件：排队期间新产生的分类
    结果不会混进来（[plan/10 §8]）。写文件与登记都不在事务里，只有两次短写事务
    （标记捕获、发布产物）。

    失败就是失败：这里不写 `runs`，导出的失败不会改动分类结果，也不通过
    classify 恢复重跑（[plan/10 §7—§8]）。
    """
    connection = database.connect()
    try:
        export = exports_repo.get_export_by_job(connection, job.job_id)
    finally:
        connection.close()
    if export is None:
        raise ExportStateError(f"导出任务 {job.job_id} 没有对应的 exports 行")
    export_id = export.export_id

    stamp = captured_at or utc_now()
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot_conn:
            run = runs_repo.get_run(snapshot_conn, job.run_id)
            if run is None:
                raise ExportStateError(f"未知 run_id：{job.run_id}")
            counts = counts_of(
                run,
                records_repo.count_by_status(snapshot_conn, job.run_id),
                records_repo.count_review_required(snapshot_conn, job.run_id),
            )
            snapshot = load_snapshot(
                snapshot_conn, job.run_id, captured_at=stamp, counts=counts
            )
            captured_revision = run.revision
            captured_run_status = run.status
    finally:
        connection.close()

    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            exports_repo.mark_captured(
                tx,
                export_id=export_id,
                captured_revision=captured_revision,
                captured_at=stamp,
                captured_run_status=captured_run_status,
                captured_counts=counts,
            )
    finally:
        connection.close()

    document = build_document(snapshot, export_id=export_id)
    artifacts = write_document(document, outputs_root=Path(outputs_root))

    connection = database.connect()
    try:
        with write_transaction(connection) as tx:
            publish_export(
                tx,
                job_id=job.job_id,
                export_id=export_id,
                artifacts=artifacts,
                created_at=utc_now(),
            )
    finally:
        connection.close()

    return ExportOutcome(
        export_id=export_id,
        run_id=job.run_id,
        artifacts=artifacts,
        stats=document.stats,
        counts=dict(counts),
    )


# ------------------------------------------- 手动入队与接口视图（S05-04）


def export_scope(run_id: str) -> str:
    """手动导出的幂等范围；按批次区分，风格同 `batches._resume_scope`。"""
    return f"POST /api/runs/{run_id}/exports"


@dataclass(frozen=True)
class ManualExport:
    """一次手动入队的结果；``reused`` 为真表示命中幂等键，没有新建任何东西。"""

    export_id: str
    job_id: str
    reused: bool = False
    http_status: int = 202


@dataclass(frozen=True)
class ArtifactDownload:
    """一份可下载的登记文件。

    ``path`` 由**数据库里的相对路径**与配置的导出根目录拼出；接口不接受调用方
    给路径，也不回显本机绝对路径。
    """

    path: Path
    download_name: str
    media_type: str


def create_manual_export(
    database: Database,
    run_id: str,
    *,
    idempotency_key: str | None = None,
    request_sha256: str | None = None,
) -> ManualExport:
    """排一份手动导出（``source='manual'``）；**只入队，不生成文件**。

    任意批次状态都可以导出（[plan/08 §7]）：快照由 worker 认领时才捕获，排队期间
    新产生的分类结果不进这一份。同一批次同时只允许一份导出在飞——两次导出争的是
    同一个执行槽，让第二份排着队只会让界面上的「最新导出」来回跳；要拿新文件就等
    这一份结束（再次导出本来就会生成新的 `export_id` 与新文件，不覆盖原文件）。

    `scheduled_revision` 记**排这份导出时**的批次 revision；`uq_automatic_export_revision`
    只约束 `source='automatic'`，所以手动导出不会顶掉终态那次自动导出。
    """
    connection = database.connect()
    try:
        if idempotency_key:
            existing = jobs_repo.get_idempotent(
                connection, scope=export_scope(run_id), key=idempotency_key
            )
            if existing is not None:
                if existing.request_sha256 != (request_sha256 or ""):
                    raise BatchError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 Idempotency-Key 已用于不同的请求体",
                        details={"key": idempotency_key, "run_id": run_id},
                    )
                return ManualExport(
                    export_id=str(existing.response_data["export_id"]),
                    job_id=str(existing.response_data["job_id"]),
                    reused=True,
                    http_status=existing.http_status,
                )

        run = runs_repo.get_run(connection, run_id)
        if run is None:
            raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)
        if jobs_repo.find_active_job(connection, run_id, kind=jobs_repo.EXPORT_KIND) is not None:
            raise BatchError(
                "STATE_CONFLICT",
                "该批次已有排队或运行中的导出任务；等它完成后可再导出一份",
                details={"run_id": run_id},
            )

        job_id = jobs_repo.new_job_id()
        export_id = exports_repo.new_export_id()
        created_at = utc_now()
        response_data = {"export_id": export_id, "job_id": job_id}
        try:
            with write_transaction(connection) as tx:
                jobs_repo.insert_job(
                    tx,
                    job_id=job_id,
                    run_id=run_id,
                    kind=jobs_repo.EXPORT_KIND,
                    mode=jobs_repo.EXPORT_MODE_MANUAL,
                    payload={},
                    created_at=created_at,
                )
                exports_repo.insert_export(
                    tx,
                    export_id=export_id,
                    job_id=job_id,
                    run_id=run_id,
                    source=SOURCE_MANUAL,
                    scheduled_revision=run.revision,
                    created_at=created_at,
                )
                if idempotency_key:
                    jobs_repo.put_idempotent(
                        tx,
                        scope=export_scope(run_id),
                        key=idempotency_key,
                        request_sha256=request_sha256 or "",
                        http_status=202,
                        response_data=response_data,
                        created_at=created_at,
                        run_id=run_id,
                        job_id=job_id,
                    )
        except sqlite3.IntegrityError as exc:
            raise BatchError(
                "STATE_CONFLICT",
                "该批次已排上同一次导出",
                details={"reason": str(exc), "run_id": run_id},
            ) from exc

        return ManualExport(export_id=export_id, job_id=job_id)
    except DatabaseBusyError as exc:
        raise BatchError("DB_BUSY", str(exc), http_status=503) from exc
    finally:
        connection.close()


def _artifact_view(artifact: exports_repo.ArtifactRow) -> dict[str, Any]:
    """一个可下载文件的接口视图；`download_url` 由 artifact_id 拼出。"""
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "download_name": artifact.download_name,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "download_url": f"/api/artifacts/{artifact.artifact_id}/download",
    }


def export_view(
    connection: sqlite3.Connection, export: exports_repo.ExportRow
) -> dict[str, Any]:
    """一份导出的接口视图：状态、捕获信息与已登记文件（[plan/08 §7]）。

    字段名照 [plan/08 §7]：`job_status`、`captured_at`、`run_revision`、
    `run_status_at_capture`、`counts_at_capture`、`artifacts`、`error`。
    **捕获前**（`queued`/`running`）后四项为 ``null``，界面据此显示「尚未捕获快照」，
    不拿 `scheduled_revision` 冒充已捕获的 revision。

    `artifacts` 只列**已登记**的文件：半组文件不发布，所以 `completed` 必然是两份、
    未完成必然为空——界面按同一规则给下载链接。文件在磁盘上被删掉属于另一回事，
    下载接口会在那时返回 404 `ARTIFACT_MISSING`。
    """
    job = jobs_repo.get_job(connection, export.job_id)
    return {
        "export_id": export.export_id,
        "job_id": export.job_id,
        "run_id": export.run_id,
        "source": export.source,
        "job_status": None if job is None else job.status,
        "created_at": export.created_at,
        "scheduled_revision": export.scheduled_revision,
        "captured_at": export.captured_at,
        "run_revision": export.captured_revision,
        "run_status_at_capture": export.captured_run_status,
        "counts_at_capture": export.captured_counts,
        "artifacts": [
            _artifact_view(artifact)
            for artifact in exports_repo.list_artifacts(connection, export.export_id)
        ],
        "error": None if job is None else job.error,
    }


def latest_export_view(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, Any] | None:
    """批次详情里的 `latest_export`；一次导出都没排过时为 ``None``。"""
    export = exports_repo.latest_export(connection, run_id)
    return None if export is None else export_view(connection, export)


#: 导出历史分页：与批次列表同口径（[plan/08 §5]）。
EXPORT_PAGE_SIZE_DEFAULT = 50
EXPORT_PAGE_SIZE_MAX = 100


def list_export_views(
    database: Database,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = EXPORT_PAGE_SIZE_DEFAULT,
) -> dict[str, Any]:
    """该批次的导出历史，新→旧；含未完成与失败的导出。"""
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            run = runs_repo.get_run(snapshot, run_id)
            if run is None:
                raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)
            rows, total = exports_repo.list_exports(
                snapshot, run_id, limit=page_size, offset=(page - 1) * page_size
            )
            items = [export_view(snapshot, row) for row in rows]
            return {
                "run_id": run_id,
                "page": page,
                "page_size": page_size,
                "total": total,
                "items": items,
            }
    except DatabaseBusyError as exc:  # pragma: no cover - 读连接重试后仍失败
        raise BatchError("DB_BUSY", str(exc), http_status=503) from exc
    finally:
        connection.close()


def artifact_download(
    database: Database, outputs_root: Path, artifact_id: str
) -> ArtifactDownload:
    """定位一份**已登记**的导出文件（[plan/08 §7]）。

    只在两种情况下给文件：登记行存在、且它在磁盘上确实是个文件。任何一步不成立都
    返回 404 `ARTIFACT_MISSING`——**绝不当成空文件下载**，那会让调用方把「文件没了」
    读成「导出一份空结果」。

    路径由数据库里的相对路径与导出根目录拼出，并复查它没有跑到根目录外面：接口
    不接受用户磁盘路径，登记行即使被改坏也不会读成任意文件。
    """
    connection = database.connect()
    try:
        artifact = exports_repo.get_artifact(connection, artifact_id)
    finally:
        connection.close()
    if artifact is None:
        raise BatchError(
            "ARTIFACT_MISSING",
            f"未登记的产物：{artifact_id}",
            http_status=404,
            details={"artifact_id": artifact_id},
        )

    root = Path(outputs_root).resolve()
    candidate = (root / artifact.relative_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise BatchError(
            "ARTIFACT_MISSING",
            "该产物文件不存在或已不可用；请重新导出一份",
            http_status=404,
            details={"artifact_id": artifact_id, "kind": artifact.kind},
        )
    return ArtifactDownload(
        path=candidate,
        download_name=artifact.download_name,
        media_type=artifact.media_type,
    )
