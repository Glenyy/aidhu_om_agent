"""评估结果接口（S07-03）。

**只读**。计算入口仍然只有 CLI 的 ``evaluate``（[plan/08] 的既有约定：网页不做
重活，长任务交给命令行或 worker）。这里只把已经算完、已经落库的评估结果摊开给
界面看，因此三个接口全是 GET、不接受任何写参数：

- ``GET /api/runs/{run_id}/evaluations``：某批次的评估历史，新→旧。
- ``GET /api/evaluations/{evaluation_id}``：单份评估的指标、达标判定、划分要点、
  冻结清单与报告正文。
- ``GET /api/evaluations/{evaluation_id}/records``：逐条对照的分页列表。

界面**不能**在这里发起评估、也不能在这里改标签：改标签是 S08 的事，而按 [plan/04]
「人工修订不计入 agent 预测」——若允许网页改标签再重算，评估的分母就不再是模型
的实际表现。这条边界靠「本模块没有 POST」来保证，不靠前端不发请求。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ...config import AppConfig
from ...services.evaluation import (
    EVALUATION_PAGE_SIZE_DEFAULT,
    EVALUATION_PAGE_SIZE_MAX,
    RECORD_PAGE_SIZE_DEFAULT,
    RECORD_PAGE_SIZE_MAX,
    EvaluationError,
    evaluation_detail,
    list_evaluation_records,
    list_run_evaluations,
)
from ...storage import Database
from ..responses import fail, success

router = APIRouter(tags=["evaluations"])


@router.get("/runs/{run_id}/evaluations")
async def list_run_evaluations_endpoint(
    request: Request,
    run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=EVALUATION_PAGE_SIZE_DEFAULT, ge=1, le=EVALUATION_PAGE_SIZE_MAX
    ),
) -> Any:
    """该批次的评估历史；同一批次可以用不同划分（或修订后）评多次，全部留痕。

    每项带 ``passed``：只有**全部**检查都达标才是 ``true``，含不可计算项时为
    ``false``——「算不出来」不能算通过。
    """
    database: Database = request.app.state.db
    try:
        payload = list_run_evaluations(
            database, run_id, page=page, page_size=page_size
        )
    except EvaluationError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


@router.get("/evaluations/{evaluation_id}")
async def get_evaluation_endpoint(request: Request, evaluation_id: str) -> Any:
    """单份评估详情。

    ``report_text`` 直接内嵌正文：评估报告不是导出产物，够不着
    `artifacts` 的下载接口；内嵌后页面可以用 Blob 存成本地文件，且不新增接口。
    ``report_files`` 同时返回文件名、大小与是否存在，便于判断「文件被手工删了」
    与「报告本来就没写出来」。
    """
    config: AppConfig = request.app.state.config
    database: Database = request.app.state.db
    try:
        payload = evaluation_detail(database, evaluation_id, config=config)
    except EvaluationError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


@router.get("/evaluations/{evaluation_id}/records")
async def list_evaluation_records_endpoint(
    request: Request,
    evaluation_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=RECORD_PAGE_SIZE_DEFAULT, ge=1, le=RECORD_PAGE_SIZE_MAX
    ),
    agree: bool | None = Query(default=None),
    status: str | None = Query(default=None),
    record_id: str | None = Query(default=None),
) -> Any:
    """逐条对照（gold 标签 vs agent 原预测），按编号升序。

    ``agree=false`` 只命中「有预测但不一致」的行。**没有预测的行**（技术失败、
    未完成）`agree` 为空，要用 ``status`` 单独看——否则会把「没判出来」混进
    「判错了」，那是两种完全不同的质量信号。
    """
    database: Database = request.app.state.db
    try:
        payload = list_evaluation_records(
            database,
            evaluation_id,
            page=page,
            page_size=page_size,
            agree=agree,
            status=status,
            record_id=record_id,
        )
    except EvaluationError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


__all__ = ["router"]
