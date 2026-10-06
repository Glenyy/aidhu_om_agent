"""S03-01：单次模型服务适配。

约定（依据 [plan/02 §4](../../../plan/02-架构与详细设计.md)）：

- 使用 OpenAI 兼容 SDK（``openai>=2,<3``）；**关闭 SDK 自带重试**
  （``max_retries=0``），重试只由阶段执行器管理，避免叠加尝试次数。
- **只解析最终回答**：响应若同时返回推理内容与最终内容，只读最终内容，
  推理内容不落盘、不返回前端。
- 返回服务**实际**返回的模型标识、用量与耗时；用量缺失为 ``None``，
  不用 0 冒充。
- 超时按阶段配置（阶段一 120 秒、阶段二 300 秒，可配置）。
- 失败按 `llm/errors.py` 分类，消息经 `config.redact()` 脱敏后才向外抛出。

本模块不读取环境变量、不写日志、不落盘；凭据只从传入的 ``AppConfig`` 取用。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import AppConfig, ModelConfig, redact
from .errors import (
    ContextLimitError,
    ModelCallError,
    ModelConfigError,
    PermanentModelError,
    RetryableModelError,
    TruncatedOutputError,
)

#: 阶段标识；同时用作配置选择键、错误标注与请求捕获的标记。
STAGE1 = "stage1"
STAGE2 = "stage2"

STAGES: tuple[str, ...] = (STAGE1, STAGE2)

#: 带这些状态码的响应视为临时故障，在阶段预算内重试。
_RETRYABLE_STATUS = frozenset({408, 425, 429})

#: 无状态码时按异常类型判定；这些是 httpx/openai 的连接与超时族。
_RETRYABLE_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "InternalServerError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "Timeout",
        "TimeoutError",
        "WriteError",
        "WriteTimeout",
    }
)


@dataclass(frozen=True)
class ModelUsage:
    """服务返回的用量；缺失的项为 ``None``。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    """一次模型调用的最终结果；``simulated`` 为 ``True`` 时非真实模型输出。"""

    content: str
    model: str
    usage: ModelUsage | None
    latency_ms: int
    finish_reason: str | None
    simulated: bool = False


class ModelClient(Protocol):
    """阶段执行器依赖的最小协议；模拟实现见 ``agent/mock_samples.py``。"""

    def call(self, messages: Sequence[Mapping[str, str]], stage: str) -> ModelResponse:
        ...


def classify_model_error(exc: BaseException, stage: str | None = None) -> ModelCallError:
    """把 SDK / 网络异常映射为本模块的错误类型。

    判定顺序：已是本模块错误 → 按 HTTP 状态码 → 按异常类型名 → 未知异常
    保守记为不可重试（不静默消耗阶段预算），并在消息中保留原异常类型名。
    """
    if isinstance(exc, ModelCallError):
        return exc

    status = getattr(exc, "status_code", None)
    status_code = status if isinstance(status, int) else None
    name = type(exc).__name__
    text = redact(str(exc)).strip() or name

    if status_code is not None:
        if status_code in _RETRYABLE_STATUS or 500 <= status_code < 600:
            return RetryableModelError(text, stage=stage, status_code=status_code)
        if status_code == 400 and "context" in text.lower():
            return ContextLimitError(text, stage=stage, status_code=status_code)
        return PermanentModelError(text, stage=stage, status_code=status_code)

    if name in _RETRYABLE_EXCEPTION_NAMES or isinstance(exc, (OSError, TimeoutError)):
        return RetryableModelError(text, stage=stage)

    return PermanentModelError(f"{name}：{text}", stage=stage)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _usage_of(usage: Any) -> ModelUsage | None:
    """把 SDK 用量对象转成 ``ModelUsage``；整体缺失返回 ``None``。"""
    if usage is None:
        return None
    result = ModelUsage(
        prompt_tokens=_int_or_none(getattr(usage, "prompt_tokens", None)),
        completion_tokens=_int_or_none(getattr(usage, "completion_tokens", None)),
        total_tokens=_int_or_none(getattr(usage, "total_tokens", None)),
    )
    if result == ModelUsage():
        return None
    return result


def _response_of(
    completion: Any, stage: str, latency_ms: int, model_hint: str
) -> ModelResponse:
    """只取第一个候选的最终内容；推理内容被有意忽略。"""
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise PermanentModelError("模型响应不含任何候选结果", stage=stage)

    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise TruncatedOutputError(
            "模型输出被长度截断，未生成完整结果；不裁剪内容也不伪造标签", stage=stage
        )

    message = getattr(choice, "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        # 空内容不在此处判失败：交由解析环节按格式错误进入有限重试。
        content = ""

    actual_model = getattr(completion, "model", None)
    return ModelResponse(
        content=content,
        model=actual_model if isinstance(actual_model, str) and actual_model else model_hint,
        usage=_usage_of(getattr(completion, "usage", None)),
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )


class OpenAICompatibleClient:
    """基于 OpenAI 兼容 SDK 的真实客户端；按阶段选择模型与超时。

    SDK 客户端**延迟构造**：只走模拟模式的进程不需要服务地址与凭据，
    首次对某阶段发起真实调用时才校验配置。
    """

    def __init__(self, config: AppConfig) -> None:
        self._models: dict[str, ModelConfig] = {
            STAGE1: config.stage1,
            STAGE2: config.stage2,
        }
        self._clients: dict[str, Any] = {}

    def _model_config(self, stage: str) -> ModelConfig:
        try:
            return self._models[stage]
        except KeyError:
            raise ValueError(f"未知阶段 {stage!r}；只接受 {STAGES[0]}/{STAGES[1]}") from None

    def _client_for(self, stage: str) -> Any:
        if stage in self._clients:
            return self._clients[stage]

        model_config = self._model_config(stage)
        if not model_config.base_url:
            raise ModelConfigError(
                f"{stage} 缺少服务地址：请在 configs/config.local.toml 填写 "
                f"models.{stage}.base_url，或设置 AIDHU_{stage.upper()}_BASE_URL",
                stage=stage,
            )
        if not model_config.api_key:
            raise ModelConfigError(
                f"{stage} 缺少凭据：请在本机 .env 设置 "
                f"AIDHU_{stage.upper()}_API_KEY 或 AIDHU_API_KEY（不写入版本库）",
                stage=stage,
            )

        try:
            from openai import OpenAI
        except ImportError as exc:  # 依赖缺失不应表现为调用失败
            raise ModelConfigError("未安装 openai SDK；请按 requirements.txt 安装", stage=stage) from exc

        client = OpenAI(
            base_url=model_config.base_url,
            api_key=model_config.api_key,
            timeout=model_config.timeout_seconds,
            max_retries=0,  # 重试统一由阶段执行器管理
        )
        self._clients[stage] = client
        return client

    def call(
        self, messages: Sequence[Mapping[str, str]], stage: str
    ) -> ModelResponse:
        """发起一次不带重试的调用；失败抛 `llm.errors` 中的分类错误。"""
        model_config = self._model_config(stage)
        client = self._client_for(stage)
        payload = [dict(message) for message in messages]

        started = time.monotonic()
        try:
            completion = client.chat.completions.create(
                model=model_config.model,
                messages=payload,
            )
        except Exception as exc:  # SDK 异常族 → 本项目错误分类
            raise classify_model_error(exc, stage) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        return _response_of(completion, stage, latency_ms, model_config.model)


__all__ = [
    "STAGE1",
    "STAGE2",
    "STAGES",
    "ModelClient",
    "ModelResponse",
    "ModelUsage",
    "OpenAICompatibleClient",
    "classify_model_error",
]
