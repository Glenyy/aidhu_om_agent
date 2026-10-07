"""任务查询接口（S03-07；S06-02 起读持久化任务）。

- ``GET /api/jobs/{job_id}``：按 `job_id` 查 `jobs` 表，返回 [plan/08 §7] 的任务字段
  （`job_id`、`run_id`、`kind`、`mode`、`status`、`created_at`、`started_at`、
  `finished_at`、`current_record_key`、`current_stage`、`error`、`result`）。

**S03-07 的 ``POST /api/judge``（判一条）已删除**：判一条由整批 + 批次记录详情取代，
界面不再持有进程内任务状态（S06 阶段文档 §0.2 第 1 项、§0.3 第 9 项）。S04 起任务本来
就落在 `jobs` 表里，S03-07 的内存注册表查不到它们——本条改动把读法收敛到唯一一处。

**``mode`` 不是模拟/真实**：判别任务是 `initial`／`resume`／`retry_failed`，导出任务是
`automatic`／`manual`。界面**不得**用它判断模拟/真实；那要看批次详情的
`call_statistics.*.simulated` 与 `execution_control.last_worker.mode`（§0.3 第 5 项）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ...services.batches import BatchError, job_detail
from ...storage import Database
from ..responses import fail, success

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> Any:
    """任务状态、当前记录与阶段、受控错误与结果标识；**不含模型推理内容**。

    任务不存在返回 404 `NOT_FOUND`——包括「刚被删掉的那个内存任务」，
    这正是不再区分「内存任务」与「库任务」的收益。
    """
    database: Database = request.app.state.db
    try:
        payload = job_detail(database, job_id)
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)
    return success(request, payload)


__all__ = ["router"]
