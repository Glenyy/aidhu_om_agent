"""HTTP 请求模型与统一响应包装。

字段命名与错误封套遵循 [plan/08 §1](../../../plan/08-API接口与数据合同.md)：
``/api`` 前缀、snake_case、成功 ``data`` + ``request_id``、错误 ``error`` + ``request_id``。

S06-02 起 `JudgeRequest`／`JudgeMode` 随 `POST /api/judge` 一并删除：判别模式不再由
请求提交，而是由 worker 的启动参数决定（S06 阶段文档 §0.2 第 1、7 项）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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
    "ResumeRequest",
    "RunCreateRequest",
    "ValidateRequest",
    "error_body",
    "ok",
]
