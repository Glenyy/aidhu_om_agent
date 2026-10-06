"""命令行入口。

已实现的入口：

- 工程验证：``--version``、``--check-config``（S01）
- 只读解析：``inspect <文件> [--sheet 名称]``（S02-05，输出预检 JSON，不写库）
- 界面服务：``serve [--host] [--port]``（S03-07，供浏览器手动验证使用）

``run``/``resume``/``export``/``evaluate`` 等业务命令在 S05 实现，本阶段不提前开发。
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version

from .config import ConfigError, LimitsConfig, default_limits, load_config
from .excel.reader import InputReadError, precheck
from .schemas.qa import ParsedInput

_PACKAGE = "aidhu-om-agent"

_IMPLEMENTED_COMMANDS = ("inspect", "serve")
_PENDING_COMMANDS = ("run", "resume", "export", "evaluate")

_NOT_IMPLEMENTED = (
    "尚未实现的业务命令：run、resume、export、evaluate。\n"
    "这些命令在 S05（导出与命令行闭环）按其阶段文档实现。\n"
    "当前可用：--version、--check-config，只读解析入口 inspect，以及界面服务 serve。"
)


def _package_version() -> str:
    try:
        return version(_PACKAGE)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _enable_utf8_when_redirected() -> None:
    """输出重定向到文件/管道时改用 UTF-8。

    Windows 下 print 到管道使用系统编码（本机为 cp936），中文会写成非 UTF-8
    字节：``inspect > out.json`` 得到的文件无法按 UTF-8 读取，json 规范也不
    接受这种编码。交互式终端保持系统编码，避免传统 cmd.exe 出现乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream.isatty():
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # 已被替换、无 reconfigure 的流
            pass


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aidhu_om_agent",
        description="AIDHU 回答判别 Agent 命令行入口。",
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
        choices=[*_IMPLEMENTED_COMMANDS, *_PENDING_COMMANDS],
        help="inspect 为已实现的只读解析入口；其余为未实现的业务命令占位",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="inspect 的输入 Excel 路径",
    )
    parser.add_argument(
        "--sheet",
        default=None,
        help="inspect 显式指定的工作表名；省略时优先 QA_REF",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=5,
        help="inspect 输出的记录摘要条数，默认 5",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="serve 的监听地址，默认 127.0.0.1（只监听本机）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="serve 的监听端口，默认 8000",
    )
    return parser


def _check_config() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    _print_json(config.snapshot())
    return 0


def _resolve_limits() -> tuple[LimitsConfig, str]:
    """保护值优先取配置；用户尚未复制 config.toml 时回落到内置默认值。"""
    try:
        return load_config().limits, "config"
    except ConfigError:
        return default_limits(), "builtin-default"


def _inspect_payload(
    parsed: ParsedInput, limits: LimitsConfig, limits_source: str, preview: int
) -> dict[str, object]:
    payload = parsed.report.model_dump()
    payload["limits"] = {
        "max_upload_bytes": limits.max_upload_bytes,
        "max_records": limits.max_records,
        "source": limits_source,
    }
    payload["records_total"] = len(parsed.records)
    payload["records_preview"] = [
        record.model_dump() for record in parsed.records[: max(preview, 0)]
    ]
    payload["records_preview_note"] = (
        f"仅展示前 {max(preview, 0)} 条；完整记录由 S04 持久化、S06 页面查询提供"
    )
    return payload


def _inspect(args: argparse.Namespace) -> int:
    if not args.input:
        print(
            "inspect 需要输入文件：python -m aidhu_om_agent inspect <文件.xlsx> [--sheet 名称]",
            file=sys.stderr,
        )
        return 2

    limits, limits_source = _resolve_limits()
    try:
        parsed = precheck(args.input, args.sheet, limits=limits)
    except InputReadError as exc:
        print(f"读取错误：{exc}", file=sys.stderr)
        return 2

    _print_json(_inspect_payload(parsed, limits, limits_source, args.preview))
    return 0 if parsed.report.status == "passed" else 1


def _serve(args: argparse.Namespace) -> int:
    """启动本地界面服务；配置必须先就绪，否则给出可执行的修正提示。"""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    import uvicorn

    from .api.app import create_app

    app = create_app(config)
    dist = config.paths.frontend_dist
    if not dist.is_dir():
        print(
            f"提示：前端构建产物不存在（{dist}）。请先在项目根目录执行 pnpm build，"
            "否则浏览器只会看到接口文档而看不到业务页面。",
            file=sys.stderr,
        )
    print(f"界面地址：http://{args.host}:{args.port}/（按 Ctrl+C 停止）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    _enable_utf8_when_redirected()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "inspect":
        return _inspect(args)

    if args.command == "serve":
        return _serve(args)

    if args.check_config:
        return _check_config()

    print(_NOT_IMPLEMENTED, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
