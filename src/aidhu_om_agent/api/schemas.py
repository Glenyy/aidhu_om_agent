"""S03-07：HTTP 请求模型与统一响应包装（最小集）。

字段命名与错误封套遵循 [plan/08 §1](../../../plan/08-API接口与数据合同.md)：
``/api`` 前缀、snake_case、成功 ``data`` + ``request_id``、错误 ``error`` + ``request_id``。

本阶段**不实现**幂等请求头、分页、批次、恢复与导出（S04／S06）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

#: 判别模式：``mock`` 为确定性合成响应（默认），``real`` 为真实模型调用。
JudgeMode = Literal["mock", "real"]


class ValidateRequest(BaseModel):
    """预检请求；``sheet_name`` 省略时按 QA_REF → 唯一可见表自动选择。"""

    sheet_name: str | None = None


class JudgeRequest(BaseModel):
    """判一条请求。

    ``record_key`` 取 `/api/uploads/{upload_id}/validate` 返回记录列表中的
    同名键（编号或来源行）。
    """

    validation_id: str
    record_key: str
    mode: JudgeMode = "mock"


def ok(data: Any, request_id: str) -> dict[str, Any]:
    """成功响应封套。"""
    return {"data": data, "request_id": request_id}


def error_body(code: str, message: str, request_id: str, details: Any = None) -> dict[str, Any]:
    """错误响应封套；``details`` 省略时不出现该字段。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error, "request_id": request_id}


__all__ = ["JudgeMode", "JudgeRequest", "ValidateRequest", "error_body", "ok"]
