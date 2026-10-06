"""S03-01：模型调用的错误分类。

本模块只做分类，**不依赖 openai SDK**；SDK 异常到本模块类型的映射在
`llm/client.py::classify_model_error`。这样阶段执行器与测试不必导入 SDK。

分类依据 [plan/02 §6](../../../plan/02-架构与详细设计.md) 的失败策略表：

- 网络、超时、429、临时服务错误 → `RetryableModelError`，由阶段执行器在
  阶段预算内退避重试。
- 认证失败、模型不存在、必要参数非法 → `PermanentModelError`，立即失败，
  不消耗常规重试预算。
- 输入上下文超限、输出截断 → 单独成类：不裁剪资料、不伪造标签，提示检查
  输入或输出限制。
- 缺少服务地址或密钥 → `ModelConfigError`，属系统性配置问题（接口层对应
  `CONFIG_INVALID`）。
"""

from __future__ import annotations


class ModelCallError(Exception):
    """模型调用失败基类。

    ``retryable`` 决定阶段执行器是否在阶段预算内重试；``code`` 用于报告与
    接口错误映射，不使用 SDK 的异常类名。
    """

    code = "MODEL_CALL_ERROR"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.status_code = status_code

    def __str__(self) -> str:
        parts = [self.message]
        if self.stage:
            parts.append(f"阶段={self.stage}")
        if self.status_code is not None:
            parts.append(f"HTTP={self.status_code}")
        return "；".join(parts)


class RetryableModelError(ModelCallError):
    """临时故障：网络、超时、429、5xx。在阶段预算内重试。"""

    code = "MODEL_RETRYABLE"
    retryable = True


class PermanentModelError(ModelCallError):
    """不可重试故障：认证失败、模型不存在、必要参数非法、未知异常。"""

    code = "MODEL_PERMANENT"


class ContextLimitError(PermanentModelError):
    """输入上下文超限；不裁剪资料，单独失败。"""

    code = "CONTEXT_LIMIT"


class TruncatedOutputError(PermanentModelError):
    """输出被长度截断；不使用被截断的内容。"""

    code = "OUTPUT_TRUNCATED"


class ModelConfigError(PermanentModelError):
    """服务地址、模型 ID 或凭据缺失；属系统性配置问题。"""

    code = "CONFIG_INVALID"

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message, stage=stage)


__all__ = [
    "ModelCallError",
    "RetryableModelError",
    "PermanentModelError",
    "ContextLimitError",
    "TruncatedOutputError",
    "ModelConfigError",
]
