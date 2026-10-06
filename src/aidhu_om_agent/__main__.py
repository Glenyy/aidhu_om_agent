"""命令行入口。

S01 只提供工程验证用的最小入口（--version / --check-config / --help）。
run/resume/export/evaluate 等业务命令在 S05 实现，本阶段不提前开发。
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version

from .config import ConfigError, load_config

_PACKAGE = "aidhu-om-agent"

_NOT_IMPLEMENTED = (
    "尚未实现的业务命令：run、resume、export、evaluate。\n"
    "这些命令在 S05（导出与命令行闭环）按其阶段文档实现。\n"
    "本版本仅提供工程验证入口，可用 --version 或 --help。"
)


def _package_version() -> str:
    try:
        return version(_PACKAGE)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aidhu_om_agent",
        description="AIDHU 回答判别 Agent 命令行入口（当前为工程骨架版本）。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"aidhu-om-agent {_package_version()}",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="读取并校验配置，打印不含凭据的配置快照",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "resume", "export", "evaluate"],
        help="业务命令占位，当前均未实现（实现在 S05）",
    )
    return parser


def _check_config() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(config.snapshot(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.check_config:
        return _check_config()

    print(_NOT_IMPLEMENTED, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
