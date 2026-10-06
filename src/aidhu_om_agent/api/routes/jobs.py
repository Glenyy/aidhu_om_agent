"""S03-07：判一条与任务查询接口（最小集）。

- ``POST /api/judge``：提交一条记录进入判别，立即返回 ``job_id``。
- ``GET /api/jobs/{job_id}``：查询任务状态、当前阶段与结果。

**`/api/judge` 是临时接口，不属于最终合同**：S06 由
``/api/runs/{run_id}/records/{record_key}`` 体系取代。任务状态为内存态，
**进程重启即丢失，本阶段不承诺中断恢复**（见 `services/judging.py`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ...agent.pipeline import record_key_of
from ...services.judging import JudgeJobRegistry
from ...services.uploads import UploadStore
from ..responses import fail, success
from ..schemas import JudgeRequest

router = APIRouter(tags=["jobs"])


@router.post("/judge", status_code=202)
async def create_judge(request: Request, payload: JudgeRequest) -> Any:
    store: UploadStore = request.app.state.uploads
    jobs: JudgeJobRegistry = request.app.state.jobs

    validation = store.get_validation(payload.validation_id)
    if validation is None:
        return fail(
            request,
            404,
            "NOT_FOUND",
            f"未知 validation_id：{payload.validation_id}；请重新预检",
        )

    parsed = validation.parsed
    if parsed.report.status != "passed":
        return fail(
            request,
            409,
            "INPUT_NOT_VALIDATED",
            "该预检为 blocked，不能创建判别；请先修正输入后重新预检",
        )

    record = next(
        (
            item
            for item in parsed.valid_records()
            if record_key_of(item) == payload.record_key
        ),
        None,
    )
    if record is None:
        available = [record_key_of(item) for item in parsed.valid_records()]
        return fail(
            request,
            404,
            "NOT_FOUND",
            f"预检结果中没有记录 {payload.record_key!r}",
            details={"available_record_keys": available},
        )

    job = jobs.submit(
        record=record,
        validation_id=payload.validation_id,
        record_key=payload.record_key,
        mode=payload.mode,
    )
    return success(
        request,
        {"job_id": job.job_id, "mode": job.mode, "status": job.status},
        status_code=202,
    )


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> Any:
    jobs: JudgeJobRegistry = request.app.state.jobs
    job = jobs.get(job_id)
    if job is None:
        return fail(request, 404, "NOT_FOUND", f"未知 job_id：{job_id}")
    return success(request, job.payload())


__all__ = ["router"]
