"""S03-07：统一成功/错误响应构造。

错误码表见 [plan/08 §8](../../../plan/08-API接口与数据合同.md)；本阶段只用到其中
与上传、预检、判一条相关的部分。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .schemas import error_body, ok


def success(request: Request, data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=ok(data, request.state.request_id))


def fail(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, request.state.request_id, details),
    )


__all__ = ["fail", "success"]
