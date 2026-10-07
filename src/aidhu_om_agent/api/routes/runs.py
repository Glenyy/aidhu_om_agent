"""批次接口（S04-02 起）。

已实现：

- ``POST /api/runs``：从一次 ``passed`` 预检创建批次与排队任务，202 返回
  ``run_id``/``job_id``；要求 ``Idempotency-Key`` 请求头（plan/08 §4）。
- ``POST /api/runs/{run_id}/resume``：普通恢复与显式重试（S04-05，plan/08 §7），
  只**入队**并把目标与重开计划写进任务，实际重开轮次由 worker 认领时落实。
- ``GET /api/runs``、``GET /api/runs/{run_id}``：批次列表与详情（S04-07，plan/08 §5），
  供界面轮询进度、核对调用统计与允许操作。
- ``POST /api/runs/{run_id}/exports``、``GET /api/runs/{run_id}/exports``：手动导出与
  导出历史（S05-04，plan/08 §7）。文件下载在 `artifacts.py`。

**写接口只入队，不调用模型**：真正的阶段调用与文件生成由独立 worker 执行（api 规则）。
**读接口不发凭据与本机路径**：`model_config` 只含模型与已验证的非敏感参数。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Header, Query, Request

from ...config import AppConfig
from ...services.batches import (
    RUN_CREATE_SCOPE,
    RUN_PAGE_SIZE_DEFAULT,
    RUN_PAGE_SIZE_MAX,
    BatchError,
    create_run,
    list_batch_runs,
    resume_run,
    run_detail,
)
from ...services.exports import (
    EXPORT_PAGE_SIZE_DEFAULT,
    EXPORT_PAGE_SIZE_MAX,
    create_manual_export,
    export_scope,
    list_export_views,
)
from ...storage import Database
from ..responses import fail, success
from ..schemas import ResumeRequest, RunCreateRequest

router = APIRouter(tags=["runs"])

#: 无幂等键时的提示；本接口是写操作，缺键即 422，不静默降级。
_MISSING_KEY_MESSAGE = (
    "创建批次需要 Idempotency-Key 请求头：一次用户动作生成一个 UUID，"
    "重试时沿用同一个键"
)

#: 恢复同属写操作，同样要求幂等键；同键重放返回第一次的入队结果。
_MISSING_RESUME_KEY_MESSAGE = (
    "恢复批次需要 Idempotency-Key 请求头：一次用户动作生成一个 UUID，"
    "重复提交时沿用同一个键，避免同一批次排进两个判别任务"
)

#: 导出同为写操作；同键重放返回第一次的入队结果，不会多排一份导出。
_MISSING_EXPORT_KEY_MESSAGE = (
    "导出需要 Idempotency-Key 请求头：一次用户动作生成一个 UUID，"
    "重复提交时沿用同一个键，避免同一批次排进两份导出"
)


def request_digest(payload: RunCreateRequest) -> str:
    """请求体摘要；与幂等键一起判断「同 key 是否同一个请求」。"""
    body = json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def resume_digest(retry_failed: bool) -> str:
    """恢复请求体摘要；按**生效值**计算，省略请求体与显式 ``false`` 同键同摘要。"""
    body = json.dumps({"retry_failed": bool(retry_failed)}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def export_digest() -> str:
    """导出请求体摘要；请求体是空对象，所以摘要是常量。

    仍按「摘要」的规则走一遍：以后若给导出加上可选参数（如只导失败项），同一个
    幂等键配不同请求体会照常撞出 `IDEMPOTENCY_CONFLICT`，不用改这里的判断。
    """
    return hashlib.sha256(b"{}").hexdigest()


@router.post("/runs", status_code=202)
async def create_run_endpoint(
    request: Request,
    payload: RunCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    config: AppConfig = request.app.state.config
    database: Database = request.app.state.db

    if not idempotency_key:
        return fail(request, 422, "PARAM_VALIDATION", _MISSING_KEY_MESSAGE)

    try:
        created = create_run(
            database,
            config,
            validation_id=payload.validation_id,
            idempotency_key=idempotency_key,
            request_sha256=request_digest(payload),
        )
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)

    return success(
        request,
        {"run_id": created.run_id, "job_id": created.job_id, "reused": created.reused},
        status_code=202,
    )


@router.get("/runs")
async def list_runs_endpoint(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=RUN_PAGE_SIZE_DEFAULT, ge=1, le=RUN_PAGE_SIZE_MAX),
    status: str | None = Query(default=None),
) -> Any:
    """批次列表；排序 `created_at, run_id` 倒序，分页默认 50、上限 100。

    `status` 由界面在 S06 使用；S04 的界面只展示列表，不提供筛选控件。
    """
    database: Database = request.app.state.db
    try:
        payload = list_batch_runs(
            database, page=page, page_size=page_size, status=status
        )
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


@router.get("/runs/{run_id}")
async def get_run_endpoint(request: Request, run_id: str) -> Any:
    """批次详情：状态、计数、进度、当前任务、调用统计、允许操作、失败摘要与最近导出。

    `latest_export` 是该批次最近一次导出（终态那次自动导出也算）；捕获前
    `captured_*` 为 null。完整导出历史见 `GET /api/runs/{run_id}/exports`。
    """
    database: Database = request.app.state.db
    try:
        payload = run_detail(database, run_id)
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run_endpoint(
    request: Request,
    run_id: str,
    payload: ResumeRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """普通恢复（默认）或显式重试失败项（``retry_failed=true``）。

    响应只表示**已入队**，不表示已经调用模型（[plan/08 §7]）：
    ``selected_records`` 是本轮目标条数，``renewed_campaigns`` 是**计划**新开的
    预算轮次数，``finalize_only=true`` 表示零模型调用、只把批次收尾。
    ``dispatch_paused=true`` 说明判别派发已被暂停，任务排队但不会被执行。
    """
    database: Database = request.app.state.db

    if not idempotency_key:
        return fail(request, 422, "PARAM_VALIDATION", _MISSING_RESUME_KEY_MESSAGE)

    retry_failed = bool(payload.retry_failed) if payload is not None else False
    try:
        resumed = resume_run(
            database,
            run_id=run_id,
            retry_failed=retry_failed,
            idempotency_key=idempotency_key,
            request_sha256=resume_digest(retry_failed),
        )
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)

    return success(request, resumed.response_data(), status_code=202)


@router.post("/runs/{run_id}/exports", status_code=202)
async def create_export_endpoint(
    request: Request,
    run_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    """排一份手动导出；响应只表示**已入队**，不表示文件已经生成。

    请求体是空对象（[plan/08 §7]）：导出读的全是已落库数据，没有可选参数。文件由
    worker 认领时才生成，因此**任意批次状态都可导出**，快照是那次认领时的进度。
    同一批次已有一份导出在飞时返回 409 `STATE_CONFLICT`（与 `allowed_actions.can_export`
    同一条规则）。
    """
    database: Database = request.app.state.db

    if not idempotency_key:
        return fail(request, 422, "PARAM_VALIDATION", _MISSING_EXPORT_KEY_MESSAGE)

    try:
        created = create_manual_export(
            database,
            run_id,
            idempotency_key=idempotency_key,
            request_sha256=export_digest(),
        )
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)

    return success(
        request,
        {"export_id": created.export_id, "job_id": created.job_id, "reused": created.reused},
        status_code=202,
    )


@router.get("/runs/{run_id}/exports")
async def list_exports_endpoint(
    request: Request,
    run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=EXPORT_PAGE_SIZE_DEFAULT, ge=1, le=EXPORT_PAGE_SIZE_MAX),
) -> Any:
    """该批次的导出历史，新→旧（[plan/08 §7]）。

    含未完成与失败的导出：界面要能显示「已入队但还没捕获快照」和「导出失败」两种
    行，所以这里不发过滤参数。`artifacts` 只在导出 `completed` 时非空。
    """
    database: Database = request.app.state.db
    try:
        payload = list_export_views(database, run_id, page=page, page_size=page_size)
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


__all__ = [
    "RUN_CREATE_SCOPE",
    "export_digest",
    "export_scope",
    "request_digest",
    "resume_digest",
    "router",
]
