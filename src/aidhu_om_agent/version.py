"""版本快照：程序、结构、输入合同、提示词与结果结构版本（S04-02）。

批次创建时把这份快照写入 `runs.versions_json`，恢复时据此判断旧批次是否仍能用
当前程序处理（[plan/08 §5](../../../plan/08-API接口与数据合同.md) 的 versions 字段）。

**不在这里读凭据**：快照只含版本与提示词正文，凭据永远不进库。
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version

from .agent.stage1 import STAGE1_PROMPT_VERSION
from .agent.stage2 import STAGE2_PROMPT_VERSION
from .prompts import load_prompt
from .schemas.analysis import STAGE1_SCHEMA_VERSION
from .schemas.judgement import STAGE2_SCHEMA_VERSION
from .schemas.qa import INPUT_CONTRACT_VERSION
from .storage.database import SCHEMA_VERSION

#: 分发名；与 pyproject 的 ``[project].name`` 一致。
PACKAGE = "aidhu-om-agent"


def package_version() -> str:
    """已安装分发的版本；源码树未安装时返回占位串，不假装是某个版本。"""
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def prompt_fingerprint(text: str) -> str:
    """提示词正文的 sha256；用于核对「恢复是否沿用同一份提示词」。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_snapshot() -> dict[str, dict[str, str]]:
    """两阶段提示词的正文、版本与摘要。

    **正文入库**是有意为之：恢复必须沿用批次创建时的提示词，而不是当时的磁盘内容。
    """
    return {
        "stage1": {
            "version": STAGE1_PROMPT_VERSION,
            "sha256": prompt_fingerprint(load_prompt("stage1")),
            "text": load_prompt("stage1"),
        },
        "stage2": {
            "version": STAGE2_PROMPT_VERSION,
            "sha256": prompt_fingerprint(load_prompt("stage2")),
            "text": load_prompt("stage2"),
        },
    }


def versions_snapshot() -> dict[str, object]:
    """写入 `runs.versions_json` 的版本集合（不含任何路径与凭据）。"""
    return {
        "program": package_version(),
        "schema": SCHEMA_VERSION,
        "input_contract": INPUT_CONTRACT_VERSION,
        "stage1_prompt": STAGE1_PROMPT_VERSION,
        "stage2_prompt": STAGE2_PROMPT_VERSION,
        "stage1_schema": STAGE1_SCHEMA_VERSION,
        "stage2_schema": STAGE2_SCHEMA_VERSION,
    }


__all__ = [
    "PACKAGE",
    "package_version",
    "prompt_fingerprint",
    "prompt_snapshot",
    "versions_snapshot",
]
