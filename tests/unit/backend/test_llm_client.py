"""S03-01 的模型客户端单测：错误分类、响应解析与延迟构造。

不发起任何真实网络调用：SDK 客户端要么被注入替身，要么只做本地构造
（``OpenAI(...)`` 构造本身不联网），因此本文件可以在无凭据环境下运行。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aidhu_om_agent.config import (
    AppConfig,
    ExecutionConfig,
    LimitsConfig,
    ModelConfig,
    PathsConfig,
)
from aidhu_om_agent.llm.client import (
    STAGE1,
    STAGE2,
    OpenAICompatibleClient,
    classify_model_error,
)
from aidhu_om_agent.llm.errors import (
    ContextLimitError,
    ModelConfigError,
    PermanentModelError,
    RetryableModelError,
    TruncatedOutputError,
)

# ---------------------------------------------------------------- 替身


class _StatusError(Exception):
    """带 HTTP 状态码的异常，模拟 openai SDK 的 APIStatusError。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class APIConnectionError(Exception):
    """与 openai SDK 同名，用于核对按异常类名的分类规则。"""


class _Completions:
    def __init__(self, *, completion: Any = None, error: BaseException | None = None) -> None:
        self._completion = completion
        self._error = error
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._completion


class _FakeSDK:
    """只有 chat.completions.create 的最小替身。"""

    def __init__(self, *, completion: Any = None, error: BaseException | None = None) -> None:
        self.completions = _Completions(completion=completion, error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def _config(*, base_url: str = "http://127.0.0.1:9/v1", api_key: str | None = "test-key") -> AppConfig:
    model = ModelConfig(
        base_url=base_url, model="test-model", timeout_seconds=1.0, api_key=api_key
    )
    return AppConfig(
        stage1=model,
        stage2=model,
        execution=ExecutionConfig(
            concurrency=1, max_attempts_per_stage_campaign=3, retry_backoff_seconds=(2, 4)
        ),
        limits=LimitsConfig(max_upload_bytes=1024, max_records=10),
        paths=PathsConfig(
            database=Path("db"),
            uploads=Path("uploads"),
            runtime=Path("runtime"),
            outputs=Path("outputs"),
            logs=Path("logs"),
            frontend_dist=Path("dist"),
        ),
        source_config=Path("config.toml"),
        source_env=None,
    )


def _client_with(sdk: _FakeSDK, *, base_url: str = "http://127.0.0.1:9/v1", api_key: str | None = "k") -> OpenAICompatibleClient:
    client = OpenAICompatibleClient(_config(base_url=base_url, api_key=api_key))
    client._clients[STAGE1] = sdk  # 注入替身，避免真实联网
    return client


def _completion(
    content: str | None,
    *,
    model: str = "server-model-v1",
    finish_reason: str = "stop",
    usage: Any = None,
    reasoning: str | None = None,
) -> Any:
    message = SimpleNamespace(content=content)
    if reasoning is not None:
        message.reasoning_content = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model=model,
        usage=usage,
    )


# ---------------------------------------------------------------- 错误分类


def test_existing_model_error_passes_through() -> None:
    error = RetryableModelError("原样", stage=STAGE1)

    assert classify_model_error(error) is error


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503, 599])
def test_retryable_status_codes(status: int) -> None:
    result = classify_model_error(_StatusError("服务暂时不可用", status), STAGE1)

    assert isinstance(result, RetryableModelError)
    assert result.retryable is True
    assert result.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_status_codes(status: int) -> None:
    result = classify_model_error(_StatusError("请求被拒绝", status), STAGE2)

    assert isinstance(result, PermanentModelError)
    assert not isinstance(result, RetryableModelError)
    assert result.status_code == status


def test_context_limit_is_its_own_class() -> None:
    result = classify_model_error(
        _StatusError("This model's maximum context length is 8192 tokens", 400), STAGE1
    )

    assert isinstance(result, ContextLimitError)
    assert result.code == "CONTEXT_LIMIT"


def test_connection_error_by_name_is_retryable() -> None:
    result = classify_model_error(APIConnectionError("连接被重置"), STAGE1)

    assert isinstance(result, RetryableModelError)


def test_os_error_is_retryable() -> None:
    assert isinstance(classify_model_error(OSError("网络不可达"), STAGE1), RetryableModelError)


def test_unrelated_exception_name_is_not_retryable() -> None:
    class NotAConnectionProblem(Exception):
        pass

    result = classify_model_error(NotAConnectionProblem("未知"), STAGE1)

    assert not result.retryable


def test_timeout_error_is_retryable() -> None:
    assert isinstance(classify_model_error(TimeoutError("超时"), STAGE2), RetryableModelError)


def test_unknown_exception_is_not_retryable_and_keeps_type_name() -> None:
    class WeirdError(Exception):
        pass

    result = classify_model_error(WeirdError("说不清"), STAGE1)

    assert isinstance(result, PermanentModelError)
    assert not result.retryable
    assert "WeirdError" in result.message


def test_secret_is_redacted_from_error_message() -> None:
    result = classify_model_error(
        _StatusError("invalid api key: sk-abcdefghijklmnop", 401), STAGE1
    )

    assert "sk-abcdefghijklmnop" not in result.message
    assert "***" in result.message


def test_stage_is_attached_to_message() -> None:
    result = classify_model_error(_StatusError("x", 429), STAGE1)

    assert "阶段=stage1" in str(result)


# ---------------------------------------------------------------- 响应解析


def test_only_final_content_is_used() -> None:
    sdk = _FakeSDK(completion=_completion('{"ok": true}', reasoning="这是推理内容"))
    client = _client_with(sdk)

    response = client.call([{"role": "user", "content": "hi"}], STAGE1)

    assert response.content == '{"ok": true}'
    assert "推理内容" not in response.content
    assert response.model == "server-model-v1"
    assert response.simulated is False


def test_truncated_output_is_rejected() -> None:
    sdk = _FakeSDK(completion=_completion('{"ok": true', finish_reason="length"))
    client = _client_with(sdk)

    with pytest.raises(TruncatedOutputError, match="截断"):
        client.call([{"role": "user", "content": "hi"}], STAGE1)


def test_missing_choices_is_permanent_error() -> None:
    sdk = _FakeSDK(completion=SimpleNamespace(choices=[], model="m", usage=None))
    client = _client_with(sdk)

    with pytest.raises(PermanentModelError, match="候选结果"):
        client.call([{"role": "user", "content": "hi"}], STAGE1)


def test_absent_usage_stays_none() -> None:
    sdk = _FakeSDK(completion=_completion("{}", usage=None))
    client = _client_with(sdk)

    assert client.call([{"role": "user", "content": "hi"}], STAGE1).usage is None


def test_present_usage_is_mapped() -> None:
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    sdk = _FakeSDK(completion=_completion("{}", usage=usage))
    client = _client_with(sdk)

    mapped = client.call([{"role": "user", "content": "hi"}], STAGE1).usage

    assert mapped is not None
    assert (mapped.prompt_tokens, mapped.completion_tokens, mapped.total_tokens) == (11, 7, 18)


def test_sdk_error_is_classified() -> None:
    sdk = _FakeSDK(error=_StatusError("rate limited", 429))
    client = _client_with(sdk)

    with pytest.raises(RetryableModelError):
        client.call([{"role": "user", "content": "hi"}], STAGE1)


def test_sdk_client_disables_its_own_retry() -> None:
    # 重试只由阶段执行器管理；SDK 自带重试会叠加尝试次数。
    client = OpenAICompatibleClient(_config())

    assert client._client_for(STAGE1).max_retries == 0
    assert client._client_for(STAGE2).max_retries == 0


# ---------------------------------------------------------------- 延迟构造


def test_missing_base_url_raises_config_error() -> None:
    client = OpenAICompatibleClient(_config(base_url=""))

    with pytest.raises(ModelConfigError, match="缺少服务地址"):
        client.call([{"role": "user", "content": "hi"}], STAGE1)


def test_missing_api_key_raises_config_error() -> None:
    client = OpenAICompatibleClient(_config(api_key=None))

    with pytest.raises(ModelConfigError, match="缺少凭据"):
        client.call([{"role": "user", "content": "hi"}], STAGE1)


def test_unknown_stage_is_rejected() -> None:
    client = OpenAICompatibleClient(_config())

    with pytest.raises(ValueError, match="未知阶段"):
        client.call([{"role": "user", "content": "hi"}], "stage3")
