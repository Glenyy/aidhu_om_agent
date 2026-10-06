"""配置、根路径与日志基础（S01-04）。

约定：

- 优先级：进程环境变量 > .env > TOML > 内置默认值。
- 根路径由包文件位置推导，不依赖当前工作目录；可用 AIDHU_PROJECT_ROOT 显式覆盖。
- 凭据只在后端运行时读取，不写入日志、快照或错误信息。

本模块不调用模型；真实凭据属于 S03 范围。
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

__all__ = [
    "AppConfig",
    "ConfigError",
    "ExecutionConfig",
    "LimitsConfig",
    "ModelConfig",
    "PathsConfig",
    "REDACTED",
    "load_config",
    "project_root",
    "redact",
    "RedactingFilter",
    "setup_logging",
]

REDACTED = "***"

_ENV_PROJECT_ROOT = "AIDHU_PROJECT_ROOT"
_ENV_CONFIG_FILE = "AIDHU_CONFIG_FILE"
_ENV_FALLBACK_KEY = "AIDHU_API_KEY"

CONFIG_FILE_NAME = "config.toml"
CONFIG_LOCAL_FILE_NAME = "config.local.toml"
CONFIG_EXAMPLE_NAME = "config.example.toml"

# 形如 sk-xxxx 的令牌；已知凭据值另行登记后按值替换。
_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")


class ConfigError(Exception):
    """配置缺失或非法。错误信息只描述字段，不回显任何凭据值。"""


def project_root() -> Path:
    """项目根目录。

    默认取包文件位置的向上两级（src/aidhu_om_agent/config.py -> 项目根）。
    以 AIDHU_PROJECT_ROOT 显式覆盖时优先使用该值，便于非源码安装方式定位。
    """
    override = os.environ.get(_ENV_PROJECT_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 凭据脱敏
# --------------------------------------------------------------------------

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """登记需要脱敏的字面值。空值忽略。"""
    if value:
        _secrets.add(value)


def redact(text: str, secrets: Iterator[str] | None = None) -> str:
    """把已知凭据值及 sk- 令牌替换为 ***。"""
    known = set(_secrets) if secrets is None else set(secrets)
    # 长值优先，避免短值先替换导致残留。
    for secret in sorted(known, key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    return _TOKEN_PATTERN.sub(REDACTED, text)


class RedactingFilter(logging.Filter):
    """在日志落地前脱敏，防止凭据经日志泄露。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


# --------------------------------------------------------------------------
# 配置结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """单阶段模型配置。api_key 不参与 repr，避免意外打印。"""

    base_url: str
    model: str
    timeout_seconds: float
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ExecutionConfig:
    concurrency: int
    max_attempts_per_stage_campaign: int
    retry_backoff_seconds: tuple[int, ...]


@dataclass(frozen=True)
class LimitsConfig:
    max_upload_bytes: int
    max_records: int


#: 内置保护值；不新增配置项，只作为缺少 configs/config.toml 时的回落。
_DEFAULT_LIMITS = LimitsConfig(max_upload_bytes=52428800, max_records=1000)


def default_limits() -> LimitsConfig:
    """返回内置保护值。

    只读解析入口（`python -m aidhu_om_agent inspect`）在用户尚未从模板复制
    `configs/config.toml` 时使用；存在配置时一律以配置值为准。
    """
    return _DEFAULT_LIMITS


@dataclass(frozen=True)
class PathsConfig:
    """全部为绝对路径，与当前工作目录无关。"""

    database: Path
    uploads: Path
    runtime: Path
    outputs: Path
    logs: Path
    frontend_dist: Path


@dataclass(frozen=True)
class AppConfig:
    stage1: ModelConfig
    stage2: ModelConfig
    execution: ExecutionConfig
    limits: LimitsConfig
    paths: PathsConfig
    source_config: Path
    source_env: Path | None

    def snapshot(self) -> dict[str, object]:
        """可安全记录/对比的配置快照；凭据只保留是否存在。"""
        return {
            "source_config": str(self.source_config),
            "source_env": str(self.source_env) if self.source_env else None,
            "stage1": {
                "base_url": self.stage1.base_url,
                "model": self.stage1.model,
                "timeout_seconds": self.stage1.timeout_seconds,
                "api_key_present": bool(self.stage1.api_key),
            },
            "stage2": {
                "base_url": self.stage2.base_url,
                "model": self.stage2.model,
                "timeout_seconds": self.stage2.timeout_seconds,
                "api_key_present": bool(self.stage2.api_key),
            },
            "execution": {
                "concurrency": self.execution.concurrency,
                "max_attempts_per_stage_campaign": (
                    self.execution.max_attempts_per_stage_campaign
                ),
                "retry_backoff_seconds": list(self.execution.retry_backoff_seconds),
            },
            "limits": {
                "max_upload_bytes": self.limits.max_upload_bytes,
                "max_records": self.limits.max_records,
            },
            "paths": {
                "database": str(self.paths.database),
                "uploads": str(self.paths.uploads),
                "runtime": str(self.paths.runtime),
                "outputs": str(self.paths.outputs),
                "logs": str(self.paths.logs),
                "frontend_dist": str(self.paths.frontend_dist),
            },
        }


# --------------------------------------------------------------------------
# 读取与合并
# --------------------------------------------------------------------------

_DEFAULTS: dict[str, object] = {
    "stage1": {"base_url": "", "model": "deepseek-v3", "timeout_seconds": 120},
    "stage2": {"base_url": "", "model": "deepseek-r1", "timeout_seconds": 300},
    "execution": {
        "concurrency": 1,
        "max_attempts_per_stage_campaign": 3,
        "retry_backoff_seconds": [2, 4],
    },
    "limits": {
        "max_upload_bytes": _DEFAULT_LIMITS.max_upload_bytes,
        "max_records": _DEFAULT_LIMITS.max_records,
    },
    "paths": {
        "database": "data/state.sqlite3",
        "uploads": "data/uploads",
        "runtime": "data/runtime",
        "outputs": "outputs",
        "logs": "logs",
        "frontend_dist": "dist/frontend",
    },
}

# 环境变量覆盖名 -> (配置段, 键)
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "AIDHU_STAGE1_BASE_URL": ("stage1", "base_url"),
    "AIDHU_STAGE1_MODEL": ("stage1", "model"),
    "AIDHU_STAGE1_TIMEOUT_SECONDS": ("stage1", "timeout_seconds"),
    "AIDHU_STAGE2_BASE_URL": ("stage2", "base_url"),
    "AIDHU_STAGE2_MODEL": ("stage2", "model"),
    "AIDHU_STAGE2_TIMEOUT_SECONDS": ("stage2", "timeout_seconds"),
    "AIDHU_CONCURRENCY": ("execution", "concurrency"),
    "AIDHU_MAX_ATTEMPTS_PER_STAGE_CAMPAIGN": (
        "execution",
        "max_attempts_per_stage_campaign",
    ),
    "AIDHU_MAX_UPLOAD_BYTES": ("limits", "max_upload_bytes"),
    "AIDHU_MAX_RECORDS": ("limits", "max_records"),
    "AIDHU_DATABASE_PATH": ("paths", "database"),
    "AIDHU_UPLOADS_PATH": ("paths", "uploads"),
    "AIDHU_RUNTIME_PATH": ("paths", "runtime"),
    "AIDHU_OUTPUTS_PATH": ("paths", "outputs"),
    "AIDHU_LOGS_PATH": ("paths", "logs"),
    "AIDHU_FRONTEND_DIST_PATH": ("paths", "frontend_dist"),
}


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser().resolve()
    from_env = os.environ.get(_ENV_CONFIG_FILE)
    if from_env:
        return Path(from_env).expanduser().resolve()

    config_dir = project_root() / "configs"
    # config.toml 为本机工作配置；config.local.toml 为不提交的个人配置。
    for name in (CONFIG_FILE_NAME, CONFIG_LOCAL_FILE_NAME):
        candidate = config_dir / name
        if candidate.is_file():
            return candidate.resolve()
    return (config_dir / CONFIG_FILE_NAME).resolve()


def _read_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        example = path.with_name(CONFIG_EXAMPLE_NAME)
        hint = (
            f'复制模板生成配置：copy "{example}" "{path}"'
            if example.is_file()
            else f"模板缺失：{example}"
        )
        raise ConfigError(f"未找到配置文件：{path}\n{hint}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件无法解析：{path}（{exc}）") from exc


def _toml_section(
    toml_data: Mapping[str, object], path: tuple[str, ...]
) -> Mapping[str, object]:
    """按嵌套键路径取配置段，例如 ("models", "stage1")。"""
    node: object = toml_data
    for key in path:
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key)
    return node if isinstance(node, Mapping) else {}


def _merged_section(
    section: str,
    toml_path: tuple[str, ...],
    toml_data: Mapping[str, object],
    env_map: Mapping[str, str],
) -> dict[str, object]:
    """按 默认值 < TOML < .env < 进程环境 合并单个配置段。"""
    merged = dict(_DEFAULTS[section])  # type: ignore[arg-type]
    merged.update(_toml_section(toml_data, toml_path))

    # env_map 已按“进程环境覆盖 .env”合并，此处再覆盖 TOML。
    for var, (owner, key) in _ENV_OVERRIDES.items():
        if owner != section:
            continue
        raw = env_map.get(var)
        if raw is None or raw == "":
            continue
        merged[key] = raw
    return merged


def _as_str(value: object, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"配置项 {path} 必须是文本")
    return str(value).strip()


def _as_positive_number(value: object, path: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {path} 必须是数字，实际类型 {type(value).__name__}") from exc
    if number <= 0:
        raise ConfigError(f"配置项 {path} 必须大于 0")
    return number


def _resolve_path(value: object, path: str, root: Path) -> Path:
    text = _as_str(value, path)
    if not text:
        raise ConfigError(f"配置项 {path} 不能为空")
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def _build_model_config(
    section: str,
    merged: Mapping[str, object],
    env_map: Mapping[str, str],
    fallback_key_env: str,
) -> ModelConfig:
    base_url = _as_str(merged.get("base_url", ""), f"{section}.base_url")
    model = _as_str(merged.get("model", ""), f"{section}.model")
    if not model:
        raise ConfigError(f"配置项 {section}.model 不能为空")
    timeout = _as_positive_number(merged.get("timeout_seconds"), f"{section}.timeout_seconds")

    key_env = _as_str(
        merged.get("api_key_env", f"AIDHU_{section.upper()}_API_KEY"),
        f"{section}.api_key_env",
    )
    # 阶段级变量缺失时回退到共享变量；凭据本阶段非必需。
    api_key = env_map.get(key_env) or env_map.get(fallback_key_env) or None
    register_secret(api_key)

    return ModelConfig(
        base_url=base_url,
        model=model,
        timeout_seconds=timeout,
        api_key=api_key,
    )


def _build_paths(merged: Mapping[str, object], root: Path) -> PathsConfig:
    return PathsConfig(
        database=_resolve_path(merged.get("database"), "paths.database", root),
        uploads=_resolve_path(merged.get("uploads"), "paths.uploads", root),
        runtime=_resolve_path(merged.get("runtime"), "paths.runtime", root),
        outputs=_resolve_path(merged.get("outputs"), "paths.outputs", root),
        frontend_dist=_resolve_path(
            merged.get("frontend_dist"), "paths.frontend_dist", root
        ),
        logs=_resolve_path(merged.get("logs"), "paths.logs", root),
    )


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """读取并校验配置。

    优先级：进程环境变量 > .env > TOML > 内置默认值。
    .env 只在本函数内读取，不写入 os.environ。
    """
    path = _resolve_config_path(config_path)
    toml_data = _read_toml(path)

    env_path = path.parent.parent / ".env"
    if not env_path.is_file():
        env_path = None
    dotenv_map = (
        {k: v for k, v in dotenv_values(env_path).items() if v is not None}
        if env_path
        else {}
    )
    # 进程环境覆盖 .env。
    env_map: dict[str, str] = {**dotenv_map, **{k: str(v) for k, v in os.environ.items()}}

    credentials = toml_data.get("credentials")
    fallback_key_env = _ENV_FALLBACK_KEY
    if isinstance(credentials, Mapping):
        fallback_key_env = _as_str(
            credentials.get("fallback_env", _ENV_FALLBACK_KEY),
            "credentials.fallback_env",
        )

    stage1 = _build_model_config(
        "stage1",
        _merged_section("stage1", ("models", "stage1"), toml_data, env_map),
        env_map,
        fallback_key_env,
    )
    stage2 = _build_model_config(
        "stage2",
        _merged_section("stage2", ("models", "stage2"), toml_data, env_map),
        env_map,
        fallback_key_env,
    )

    execution_merged = _merged_section("execution", ("execution",), toml_data, env_map)
    concurrency = _as_positive_number(
        execution_merged.get("concurrency"), "execution.concurrency"
    )
    attempts = _as_positive_number(
        execution_merged.get("max_attempts_per_stage_campaign"),
        "execution.max_attempts_per_stage_campaign",
    )
    backoff_raw = execution_merged.get("retry_backoff_seconds", [])
    if not isinstance(backoff_raw, (list, tuple)):
        raise ConfigError("配置项 execution.retry_backoff_seconds 必须是数字列表")
    backoff = tuple(
        int(_as_positive_number(item, "execution.retry_backoff_seconds"))
        for item in backoff_raw
    )

    limits_merged = _merged_section("limits", ("limits",), toml_data, env_map)
    limits = LimitsConfig(
        max_upload_bytes=int(
            _as_positive_number(limits_merged.get("max_upload_bytes"), "limits.max_upload_bytes")
        ),
        max_records=int(
            _as_positive_number(limits_merged.get("max_records"), "limits.max_records")
        ),
    )

    return AppConfig(
        stage1=stage1,
        stage2=stage2,
        execution=ExecutionConfig(
            concurrency=int(concurrency),
            max_attempts_per_stage_campaign=int(attempts),
            retry_backoff_seconds=backoff,
        ),
        limits=limits,
        paths=_build_paths(
            _merged_section("paths", ("paths",), toml_data, env_map), project_root()
        ),
        source_config=path,
        source_env=env_path,
    )


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------


def setup_logging(config: AppConfig, level: int = logging.INFO) -> logging.Logger:
    """配置根日志：写 logs 目录并输出到控制台，落地前统一脱敏。"""
    config.paths.logs.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redacting = RedactingFilter()

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(
        config.paths.logs / "aidhu_om_agent.log", encoding="utf-8"
    )
    stream_handler = logging.StreamHandler()
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redacting)
        root.addHandler(handler)

    return root
