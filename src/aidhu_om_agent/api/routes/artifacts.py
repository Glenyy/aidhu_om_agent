"""已登记导出文件的下载接口（S05-04）。

``GET /api/artifacts/{artifact_id}/download``：按 **artifact_id** 定位一份已登记
文件（[plan/08 §7]），不接收用户磁盘路径，也不回显本机路径。文件不存在时返回
404 ``ARTIFACT_MISSING``，**不当成空文件下载**——那会让调用方把「文件没了」读成
「导出一份空结果」。

只有成对发布的导出才会有登记行（半组文件不登记），所以能下载到这里的文件必然是
一次完整导出里的那两份之一。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ...config import AppConfig
from ...services.batches import BatchError
from ...services.exports import artifact_download
from ...storage import Database
from ..responses import fail

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact_endpoint(request: Request, artifact_id: str) -> Any:
    """下载一份已登记的导出文件。

    响应头里的文件名用登记时的 `download_name`（中文可读名 + 短 id），不是磁盘上
    的相对路径；`media_type` 也取自登记行，与产物种类一致。
    """
    config: AppConfig = request.app.state.config
    database: Database = request.app.state.db

    try:
        target = artifact_download(database, config.paths.outputs, artifact_id)
    except BatchError as exc:
        return fail(request, exc.http_status, exc.code, exc.message, exc.details or None)

    return FileResponse(
        target.path,
        media_type=target.media_type,
        filename=target.download_name,
    )


__all__ = ["router"]
