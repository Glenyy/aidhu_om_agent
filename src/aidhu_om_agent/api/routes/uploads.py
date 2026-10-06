"""S03-07：上传与预检接口。

- ``POST /api/uploads``：保存 .xlsx，返回 upload_id 与工作表清单。**不调用模型**。
- ``POST /api/uploads/{upload_id}/validate``：复用 `excel.reader.precheck()` 产出
  预检计数、阻断项、单条问题与有效记录列表。**不写库、不改写原文件**。

字段对齐 [plan/08 §3](../../../../plan/08-API接口与数据合同.md)；本阶段不实现
幂等请求头、批次创建与分页（S04／S06）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Request, UploadFile

from ...agent.pipeline import record_key_of
from ...config import AppConfig
from ...excel.reader import InputReadError, precheck, select_sheet, sheet_catalog
from ...schemas.qa import ParsedInput, QARecord
from ...services.uploads import UploadStore, UploadTooLargeError
from ..responses import fail, success
from ..schemas import ValidateRequest

router = APIRouter(tags=["uploads"])

_Q_PREVIEW_LIMIT = 60


def _q_preview(record: QARecord) -> str | None:
    """问题摘要；保留完整 q 的入口在记录详情（S06）。"""
    if record.q is None:
        return None
    text = record.q.strip()
    return text[:_Q_PREVIEW_LIMIT] + ("…" if len(text) > _Q_PREVIEW_LIMIT else "")


def _record_summary(record: QARecord) -> dict[str, Any]:
    return {
        "record_key": record_key_of(record),
        "record_id": record.record_id,
        "source_row": record.source_row,
        "order_index": record.order_index,
        "q_preview": _q_preview(record),
        "ref_count": sum(1 for text in record.refs.values() if text),
    }


def _validation_payload(
    upload_id: str, validation_id: str, parsed: ParsedInput
) -> dict[str, Any]:
    report = parsed.report
    return {
        "validation_id": validation_id,
        "upload_id": upload_id,
        "status": report.status,
        "sheet_name": report.sheet_name,
        "input_contract_version": report.input_contract_version,
        "file_sha256": report.file_sha256,
        "counts": {
            "total": report.counts.total,
            "valid": report.counts.valid,
            "input_invalid": report.counts.input_invalid,
            "skipped_blank_rows": report.counts.skipped_blank_rows,
        },
        "blockers": [blocker.model_dump(mode="json") for blocker in report.blockers],
        "row_errors": [error.model_dump(mode="json") for error in report.row_errors],
        "warnings": [warning.model_dump(mode="json") for warning in report.warnings],
        "records": [_record_summary(record) for record in parsed.valid_records()],
    }


@router.post("/uploads", status_code=201)
async def create_upload(request: Request, file: UploadFile = File(...)) -> Any:
    config: AppConfig = request.app.state.config
    store: UploadStore = request.app.state.uploads

    original_filename = file.filename or "upload.xlsx"
    if not original_filename.lower().endswith(".xlsx"):
        return fail(request, 422, "BAD_WORKBOOK", "只接受 .xlsx 文件")

    try:
        record = store.save(
            original_filename,
            file.file,
            max_bytes=config.limits.max_upload_bytes,
        )
    except UploadTooLargeError as exc:
        return fail(request, 413, "FILE_TOO_LARGE", str(exc))
    finally:
        await file.close()

    try:
        catalog = sheet_catalog(record.path)
    except InputReadError as exc:
        return fail(request, 422, "BAD_WORKBOOK", str(exc))

    return success(
        request,
        {
            "upload_id": record.upload_id,
            "original_filename": record.original_filename,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "sheets": [{"name": name, "visible": visible} for name, visible in catalog],
            "suggested_sheet": select_sheet(catalog, None).name,
        },
        status_code=201,
    )


@router.post("/uploads/{upload_id}/validate")
async def validate_upload(
    request: Request, upload_id: str, payload: ValidateRequest
) -> Any:
    config: AppConfig = request.app.state.config
    store: UploadStore = request.app.state.uploads

    upload = store.get_upload(upload_id)
    if upload is None:
        return fail(request, 404, "NOT_FOUND", f"未知 upload_id：{upload_id}")

    try:
        parsed = precheck(upload.path, payload.sheet_name, limits=config.limits)
    except InputReadError as exc:
        return fail(request, 422, "BAD_WORKBOOK", str(exc))

    validation = store.put_validation(upload_id, parsed)
    # 预检 blocked 是结构化结论，仍用 200 返回，不与参数错误混淆。
    return success(request, _validation_payload(upload_id, validation.validation_id, parsed))


__all__ = ["_record_summary", "_validation_payload", "router"]
