"""模型服务适配（S03-01）：单次调用的协议、真实实现与错误分类。"""

from .client import (
    STAGE1,
    STAGE2,
    STAGES,
    ModelClient,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleClient,
    classify_model_error,
)
from .errors import (
    ContextLimitError,
    ModelCallError,
    ModelConfigError,
    PermanentModelError,
    RetryableModelError,
    TruncatedOutputError,
)

__all__ = [
    "STAGE1",
    "STAGE2",
    "STAGES",
    "ModelClient",
    "ModelResponse",
    "ModelUsage",
    "OpenAICompatibleClient",
    "classify_model_error",
    "ModelCallError",
    "RetryableModelError",
    "PermanentModelError",
    "ContextLimitError",
    "TruncatedOutputError",
    "ModelConfigError",
]
