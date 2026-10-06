"""提示词资源加载（S03-02、S03-03）。

提示词正文只放在同目录的 ``.md`` 文件里，Python 代码不内联复制一份，
避免两处漂移。这些文件随包分发（``pyproject.toml`` 的 wheel 目标包含整包）。
"""

from __future__ import annotations

from importlib.resources import files

#: 阶段标识 → 提示词文件名。
PROMPT_FILES: dict[str, str] = {
    "stage1": "stage1.md",
    "stage2": "stage2.md",
}


def load_prompt(stage: str) -> str:
    """读取某阶段的提示词正文；未知阶段抛 ``ValueError``。"""
    try:
        filename = PROMPT_FILES[stage]
    except KeyError:
        known = "、".join(PROMPT_FILES)
        raise ValueError(f"未知阶段 {stage!r}；只接受 {known}") from None
    return files(__package__).joinpath(filename).read_text(encoding="utf-8").strip()


__all__ = ["PROMPT_FILES", "load_prompt"]
