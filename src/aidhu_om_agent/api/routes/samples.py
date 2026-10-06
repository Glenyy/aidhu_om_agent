"""S03-07：合成样例下载接口（界面验证的输入来源）。

- ``GET /api/samples``：列出可用样例的名称与用途。
- ``GET /api/samples/{name}``：返回现场生成的 .xlsx 文件本体。

仓库不提交二进制 .xlsx，界面验证所需的样例因此在这里生成。**只读**：不写库、
不调用模型、不改写任何已有文件。下载响应是文件本体，**不使用** ``data``/``error``
封套（浏览器按附件处理），但同样带 ``X-Request-ID``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from ...excel.samples import SAMPLES, build_workbook_bytes, get_sample
from ..responses import fail, success

router = APIRouter(tags=["samples"])

#: 下载响应的媒体类型；与上传校验接受的扩展名对应。
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/samples")
async def list_samples(request: Request) -> Any:
    return success(
        request,
        {
            "samples": [
                {
                    "name": sample.name,
                    "filename": sample.filename,
                    "description": sample.description,
                    "record_count": sample.record_count,
                }
                for sample in SAMPLES
            ]
        },
    )


@router.get("/samples/{name}")
async def download_sample(request: Request, name: str) -> Any:
    sample = get_sample(name)
    if sample is None:
        available = "、".join(item.name for item in SAMPLES)
        return fail(request, 404, "NOT_FOUND", f"未知样例 {name!r}；可用样例：{available}")

    content = build_workbook_bytes(sample)
    return Response(
        content=content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{sample.filename}"',
            "X-Request-ID": request.state.request_id,
            "Content-Length": str(len(content)),
        },
    )


__all__ = ["router"]
