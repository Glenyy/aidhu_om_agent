"""S01-04 配置、根路径与日志基础的单测。

覆盖：优先级（进程 env > .env > TOML > 默认值）、凭据回退、
路径与工作目录无关、缺失/非法字段的明确错误、日志脱敏、快照不含凭据。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from aidhu_om_agent import config as config_module
from aidhu_om_agent.config import ConfigError, load_config, project_root, redact

SAMPLE_TOML = """\
[models.stage1]
base_url = "https://toml.example/v1"
model = "toml-stage1"
api_key_env = "AIDHU_STAGE1_API_KEY"
timeout_seconds = 120

[models.stage2]
base_url = ""
model = "toml-stage2"
api_key_env = "AIDHU_STAGE2_API_KEY"
timeout_seconds = 300

[credentials]
fallback_env = "AIDHU_API_KEY"

[execution]
concurrency = 1
max_attempts_per_stage_campaign = 3
retry_backoff_seconds = [2, 4]

[limits]
max_upload_bytes = 52428800
max_records = 1000

[paths]
database = "data/state.sqlite3"
uploads = "data/uploads"
runtime = "data/runtime"
outputs = "outputs"
logs = "logs"
frontend_dist = "dist/frontend"
"""


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离全局脱敏登记表，避免测试间互相污染。"""
    monkeypatch.setattr(config_module, "_secrets", set())


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """构造一个 <root>/configs/config.toml + <root>/.env 的最小项目。"""
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "config.toml").write_text(SAMPLE_TOML, encoding="utf-8")
    return tmp_path


def _write_dotenv(project_dir: Path, content: str) -> None:
    (project_dir / ".env").write_text(content, encoding="utf-8")


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AIDHU_API_KEY",
        "AIDHU_STAGE1_API_KEY",
        "AIDHU_STAGE2_API_KEY",
        "AIDHU_STAGE1_BASE_URL",
        "AIDHU_STAGE1_MODEL",
        "AIDHU_STAGE1_TIMEOUT_SECONDS",
        "AIDHU_CONCURRENCY",
        "AIDHU_MAX_RECORDS",
        "AIDHU_LOGS_PATH",
        "AIDHU_PROJECT_ROOT",
        "AIDHU_CONFIG_FILE",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- 读取与默认值


def test_reads_toml_values(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.stage1.base_url == "https://toml.example/v1"
    assert cfg.stage1.model == "toml-stage1"
    assert cfg.stage2.model == "toml-stage2"
    assert cfg.execution.retry_backoff_seconds == (2, 4)
    assert cfg.limits.max_records == 1000


def test_falls_back_to_builtin_defaults(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    # 去掉 [limits]，应落到内置默认值。
    minimal = SAMPLE_TOML.split("[limits]")[0]
    (project / "configs" / "config.toml").write_text(minimal, encoding="utf-8")

    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.limits.max_records == 1000
    assert cfg.limits.max_upload_bytes == 52428800


def test_missing_config_file_gives_clear_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "config.example.toml").write_text(SAMPLE_TOML, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "configs" / "config.toml")

    message = str(excinfo.value)
    assert "未找到配置文件" in message
    assert "config.example.toml" in message


def test_invalid_toml_gives_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    (tmp_path / "configs").mkdir()
    bad = tmp_path / "configs" / "config.toml"
    bad.write_text("this is not = = toml", encoding="utf-8")

    with pytest.raises(ConfigError, match="无法解析"):
        load_config(bad)


# ---------------------------------------------------------------- 优先级


def test_process_env_overrides_toml(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIDHU_STAGE1_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("AIDHU_STAGE1_MODEL", "env-stage1")

    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.stage1.base_url == "https://env.example/v1"
    assert cfg.stage1.model == "env-stage1"
    # 未覆盖的项仍取 TOML。
    assert cfg.stage1.timeout_seconds == 120


def test_dotenv_below_process_env(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    _write_dotenv(project, "AIDHU_STAGE1_MODEL=dotenv-stage1\n")

    cfg = load_config(project / "configs" / "config.toml")
    assert cfg.stage1.model == "dotenv-stage1"

    # 进程环境应压过 .env。
    monkeypatch.setenv("AIDHU_STAGE1_MODEL", "process-stage1")
    cfg = load_config(project / "configs" / "config.toml")
    assert cfg.stage1.model == "process-stage1"


def test_dotenv_overrides_toml(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    _write_dotenv(project, "AIDHU_MAX_RECORDS=42\n")

    cfg = load_config(project / "configs" / "config.toml")
    assert cfg.limits.max_records == 42


def test_dotenv_does_not_write_os_environ(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_dotenv(project, "AIDHU_STAGE1_MODEL=dotenv-stage1\n")

    load_config(project / "configs" / "config.toml")

    import os

    assert "AIDHU_STAGE1_MODEL" not in os.environ


# ---------------------------------------------------------------- 凭据回退


def test_stage_key_used_when_present(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIDHU_STAGE1_API_KEY", "stage1-secret-value")
    monkeypatch.setenv("AIDHU_STAGE2_API_KEY", "stage2-secret-value")
    monkeypatch.setenv("AIDHU_API_KEY", "shared-secret-value")

    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.stage1.api_key == "stage1-secret-value"
    assert cfg.stage2.api_key == "stage2-secret-value"


def test_stage_key_falls_back_to_shared_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIDHU_API_KEY", "shared-secret-value")

    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.stage1.api_key == "shared-secret-value"
    assert cfg.stage2.api_key == "shared-secret-value"


def test_credentials_optional_in_s01(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)

    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.stage1.api_key is None
    assert cfg.stage2.api_key is None


def test_snapshot_never_contains_secret(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIDHU_API_KEY", "shared-secret-value")

    cfg = load_config(project / "configs" / "config.toml")
    snapshot = cfg.snapshot()

    assert "shared-secret-value" not in repr(snapshot)
    assert snapshot["stage1"]["api_key_present"] is True  # type: ignore[index]
    assert "api_key" not in snapshot["stage1"]  # type: ignore[operator]


def test_repr_of_config_hides_secret(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIDHU_API_KEY", "shared-secret-value")

    cfg = load_config(project / "configs" / "config.toml")

    assert "shared-secret-value" not in repr(cfg.stage1)


# ---------------------------------------------------------------- 路径


def _expected_root() -> Path:
    return project_root()


def test_paths_are_absolute_and_rooted(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)

    cfg = load_config(project / "configs" / "config.toml")

    assert cfg.paths.database.is_absolute()
    assert cfg.paths.database == _expected_root() / "data" / "state.sqlite3"
    assert cfg.paths.frontend_dist == _expected_root() / "dist" / "frontend"


def test_paths_independent_of_cwd(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    config_file = project / "configs" / "config.toml"

    monkeypatch.chdir(project)
    first = load_config(config_file).snapshot()["paths"]

    monkeypatch.chdir(_expected_root())
    second = load_config(config_file).snapshot()["paths"]

    monkeypatch.chdir(Path.home())
    third = load_config(config_file).snapshot()["paths"]

    assert first == second == third


def test_project_root_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIDHU_PROJECT_ROOT", str(tmp_path))
    assert project_root() == tmp_path.resolve()


# ---------------------------------------------------------------- 缺失/非法字段


def test_empty_model_raises_without_leaking_secret(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIDHU_API_KEY", "shared-secret-value")
    monkeypatch.setenv("AIDHU_STAGE2_MODEL", "")

    bad = project / "configs" / "config.toml"
    bad.write_text(SAMPLE_TOML.replace('model = "toml-stage2"', 'model = ""'), encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(bad)

    message = str(excinfo.value)
    assert "stage2.model" in message
    assert "shared-secret-value" not in message


def test_non_positive_timeout_rejected(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    bad = project / "configs" / "config.toml"
    bad.write_text(
        SAMPLE_TOML.replace("timeout_seconds = 120", "timeout_seconds = 0"), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="stage1.timeout_seconds"):
        load_config(bad)


def test_non_numeric_concurrency_rejected(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    bad = project / "configs" / "config.toml"
    bad.write_text(
        SAMPLE_TOML.replace("concurrency = 1", 'concurrency = "many"'), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="execution.concurrency"):
        load_config(bad)


# ---------------------------------------------------------------- 脱敏与日志


def test_redact_replaces_registered_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_secrets", {"super-secret-token"})

    assert redact("value=super-secret-token;") == "value=***;"


def test_redact_masks_sk_tokens() -> None:
    assert redact("key sk-abcdef1234567890 end") == "key *** end"


def test_logging_redacts_secret(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    logs_dir = tmp_path / "logs-out"
    monkeypatch.setenv("AIDHU_LOGS_PATH", str(logs_dir))
    monkeypatch.setenv("AIDHU_API_KEY", "shared-secret-value")

    cfg = load_config(project / "configs" / "config.toml")

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        config_module.setup_logging(cfg, level=logging.INFO)
        logging.getLogger("aidhu.test").info(
            "使用凭据 shared-secret-value 与 sk-abcdef1234567890 调用"
        )
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

    log_file = logs_dir / "aidhu_om_agent.log"
    content = log_file.read_text(encoding="utf-8")

    assert "shared-secret-value" not in content
    assert "sk-abcdef1234567890" not in content
    assert "***" in content
