"""S03-07：HTTP 请求模型与统一响应包装（最小集）。

字段命名与错误封套遵循 [plan/08 §1](../../../plan/08-API接口与数据合同.md)：
``/api`` 前缀、snake_case、成功 ``data`` + ``request_id``、错误 ``error`` + ``request_id``。

本阶段**不实现**分页、批次列表与详情、导出（S04-07／S06）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

#: 判别模式：``mock`` 为确定性合成响应（默认），``real`` 为真实模型调用。
JudgeMode = Literal["mock", "real"]


class ValidateRequest(BaseModel):
    """预检请求；``sheet_name`` 省略时按 QA_REF → 唯一可见表自动选择。"""

    sheet_name: str | None = None


class RunCreateRequest(BaseModel):
    """创建批次请求（S04-02）。

    **只含 validation_id**：模型、地址、超时与提示词由后端读取配置与代码后冻结，
    浏览器不提交（[plan/08 §4](../../../plan/08-API接口与数据合同.md)）。
    """

    validation_id: str


class ResumeRequest(BaseModel):
    """恢复批次请求（S04-05，[plan/08 §7](../../../plan/08-API接口与数据合同.md)）。

    ``retry_failed=false``（默认）只继续**剩余预算够用**的未完成阶段；
    ``true`` 另外纳入需要新一轮预算的可重试失败项，并为它们各开一轮新预算。
    两种情况都**不会**纳入输入失败行与 `CONTEXT_LIMIT`／`OUTPUT_TRUNCATED`
    这类必须改输入或改限制的问题（那些只能新建批次）。
    """

    retry_failed: bool = False


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


__all__ = [
    "JudgeMode",
    "JudgeRequest",
    "ResumeRequest",
    "RunCreateRequest",
    "ValidateRequest",
    "error_body",
    "ok",
]
