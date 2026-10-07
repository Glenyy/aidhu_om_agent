"""命令行入口。

已实现的入口：

- 工程验证：``--version``、``--check-config``（S01）
- 只读解析：``inspect <文件> [--sheet 名称]``（S02-05，输出预检 JSON，不写库）
- 界面服务：``serve [--host] [--port]``（S03-07，供浏览器手动验证使用）
- 队列 worker：``worker [--mode mock|real] [--once]``（S04-04，唯一消费者）
- 业务闭环：``run <文件.xlsx>``、``resume <run_id>``、``export <run_id>``（S05-06，
  与网页走**同一套服务**：命令只入队，执行仍由独立 worker 完成）

``evaluate`` 在本阶段**只定参数合同**：``--help`` 可见四个参数，运行到实现处提示
「未实现，将在 S07 提供」并返回 2；指标计算属 S07，这里不写任何计算逻辑。

CLI 与网页的分工见 [api 规则](../../.claude/rules/api.md)：网页和 CLI 复用服务。
因此 ``run`` 不是「另起一套执行」——它上传、预检、创建批次，然后交给 worker。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from .config import (
    CONFIG_FILE_NAME,
    CONFIG_LOCAL_FILE_NAME,
    AppConfig,
    ConfigError,
    LimitsConfig,
    default_limits,
    load_config,
    project_root,
)
from .excel.reader import InputReadError, precheck
from .schemas.qa import ParsedInput
from .version import package_version

_IMPLEMENTED_COMMANDS = ("inspect", "serve", "worker", "run", "resume", "export")
#: 只在 argparse 中定契约、实现属 S07 的命令。
_PENDING_COMMANDS = ("evaluate",)

_NOT_IMPLEMENTED = (
    "未实现，将在 S07 提供：本阶段只确定 evaluate 的参数合同"
    "（--run-id、--gold、--split-manifest、--split），不计算任何指标。"
)

_USAGE_HINT = (
    "没有指定命令。可用：--version、--check-config；"
    "inspect <文件.xlsx>（只读预检）；run <文件.xlsx>（上传并建批次）；"
    "resume <run_id>、export <run_id>；serve（界面服务）；worker（队列消费者）。\n"
    "加 --help 查看全部参数。"
)

#: 退出码表（S05 阶段文档 §0.2 第 12 项）。
#: worker 命令：3 表示独占锁被占用（**队列状态未被改动**）。
EXIT_LOCK_HELD = 3
#: ``--wait`` 时独占锁空闲：没有 worker 在跑，等下去不会有结果。
EXIT_NO_WORKER = 4
#: 任务到达终态但不算成功（`partial_failed` / `failed` / `interrupted`）。
EXIT_JOB_UNSUCCESSFUL = 5

#: 任务终态；与 `jobs` 表的状态集合一致。
_TERMINAL_JOB_STATUSES = ("completed", "partial_failed", "failed", "interrupted")

#: 默认等待轮询间隔（秒）；``--wait`` 与 worker 空闲轮询共用 `--poll-seconds`。
DEFAULT_WAIT_SECONDS = 1.0


def _package_version() -> str:
    return package_version()


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
        help=(
            "已实现：inspect(只读解析)／serve(界面服务)／worker(队列消费者)／"
            "run(上传并建批次)／resume(恢复)／export(排一份导出)；"
            "evaluate 只定参数合同，实现属 S07"
        ),
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="inspect/run 的输入 Excel 路径；resume/export 的批次标识 run_id",
    )
    parser.add_argument(
        "--sheet",
        default=None,
        help="inspect/run 显式指定的工作表名；省略时优先 QA_REF",
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
    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default="mock",
        help="worker 的执行模式；默认 mock（模拟，零真实调用与费用）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="worker 消费完当前队列后退出，不常驻轮询",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="worker 无任务时的轮询间隔；run/resume/export --wait 轮询任务状态的间隔（秒）。默认 1.0",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "配置文件路径（仅 run/resume/export）；省略时优先 configs/config.local.toml，"
            "其次 configs/config.toml"
        ),
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help=(
            "run/resume/export 等待本命令入队的那个任务到达终态；"
            "退出码 0=成功、4=没有 worker 在跑、5=终态但未成功"
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="resume 显式重试失败项；省略时只继续还有剩余预算的阶段",
    )
    parser.add_argument("--run-id", default=None, help="evaluate 的批次标识（S07）")
    parser.add_argument("--gold", default=None, help="evaluate 的人工核定答案文件（S07）")
    parser.add_argument(
        "--split-manifest", default=None, help="evaluate 的切分清单文件（S07）"
    )
    parser.add_argument("--split", default=None, help="evaluate 使用的切分名（S07）")
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


def _worker(args: argparse.Namespace) -> int:
    """消费判别队列；锁被占用时**不动队列**并以 3 退出。"""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    from .worker import DEFAULT_POLL_SECONDS, Worker, WorkerError, WorkerLockError

    poll_seconds = (
        DEFAULT_POLL_SECONDS if args.poll_seconds is None else args.poll_seconds
    )
    try:
        worker = Worker(config, mode=args.mode, poll_seconds=poll_seconds)
    except WorkerError as exc:
        print(f"worker 无法启动：{exc}", file=sys.stderr)
        return 1

    if args.mode == "real":
        print(
            "警告：real 模式会产生**真实模型调用与费用**。本阶段（S04）的真实调用"
            "预算已用满，除非另有授权，请使用默认的 mock 模式。",
            file=sys.stderr,
        )

    try:
        recovery = worker.start()
    except WorkerLockError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_LOCK_HELD
    except WorkerError as exc:
        print(f"worker 启动失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"worker {worker.worker_id} 已启动（模式 {args.mode}）；"
        f"数据库 {config.paths.database}"
    )
    if recovery.changed:
        print(
            "启动恢复："
            f"调用标记中断 {recovery.attempts_marked_unknown} 条、"
            f"任务标记 interrupted {recovery.jobs_marked_interrupted} 个、"
            f"批次标记 interrupted {recovery.runs_marked_interrupted} 个"
        )
    if args.once:
        print("--once：消费完当前队列后退出（不常驻轮询）")
    else:
        print("按 Ctrl+C 停止；退出时保留未消费的 queued 任务。")

    try:
        processed = worker.run_forever(max_idle_rounds=1 if args.once else None)
    except KeyboardInterrupt:
        print("\n收到中断，正在释放 worker 锁……", file=sys.stderr)
        return 0
    finally:
        worker.stop()

    if args.once:
        print(f"本次消费任务数：{processed}")
    return 0


# --------------------------------------------------- S05-06：业务命令与 --wait


def _cli_config_path(explicit: str | None) -> Path | None:
    """``run``/``resume``/``export`` 的配置来源。

    优先级（S05 阶段文档 §0.2 第 24 项）：``--config`` > ``configs/config.local.toml``
    > ``configs/config.toml``。返回 ``None`` 时交给 `load_config` 走内置解析，报错
    文案（含复制模板的提示）与 `serve`/`worker` 完全一致。

    **不改已验收命令的行为**：`serve`/`worker` 仍用 `load_config()` 的默认顺序
    （那里 `config.toml` 优先），只有新增的三个命令按上面的顺序取配置。
    """
    if explicit:
        return Path(explicit).expanduser()
    config_dir = project_root() / "configs"
    for name in (CONFIG_LOCAL_FILE_NAME, CONFIG_FILE_NAME):
        candidate = config_dir / name
        if candidate.is_file():
            return candidate
    return None


def _cli_config(args: argparse.Namespace) -> AppConfig | None:
    """读取配置；失败时打印原因并返回 ``None``（调用方返回退出码 1）。"""
    try:
        return load_config(_cli_config_path(args.config))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return None


def _open_database(config: AppConfig):
    """打开库并迁移到最新；`initialize()` 幂等，与界面服务/worker 用同一条路径。"""
    from .storage import Database

    database = Database(config.paths.database)
    database.initialize()
    return database


def _worker_lock(config: AppConfig):
    """worker 独占锁对象；只用它的 `probe()` 探测，**不加锁、不写归属信息**。"""
    from .worker import LOCK_FILENAME, WorkerLock

    return WorkerLock(config.paths.runtime / LOCK_FILENAME)


def _digest(payload: dict[str, object]) -> str:
    """请求体摘要；与 `api/routes/runs.py` 的摘要同法，便于两边对照。

    幂等键只在**同一命令的同一次重试**内起作用，而 CLI 每次运行都生成新键，
    因此这里只要求与本进程写入的键自洽，不跨进程共享摘要。
    """
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _wait_seconds(args: argparse.Namespace) -> float:
    value = DEFAULT_WAIT_SECONDS if args.poll_seconds is None else float(args.poll_seconds)
    return max(value, 0.05)  # 负值会让 time.sleep 抛异常


def _no_worker_message() -> int:
    print(
        "未检测到运行中的 worker（独占锁空闲）：已入队的任务不会被执行，等下去也不会有结果。\n"
        "请另开一个终端启动消费者，再重新执行本命令：\n"
        "  python -m aidhu_om_agent worker --mode mock",
        file=sys.stderr,
    )
    return EXIT_NO_WORKER


def _read_job(database, job_id: str):
    from .repositories import jobs as jobs_repo

    connection = database.connect()
    try:
        return jobs_repo.get_job(connection, job_id)
    finally:
        connection.close()


def _report_job_outcome(job) -> int:
    """终态任务的退出反馈：只有 `completed` 算成功。

    「需复核」标记本身不改变成功状态（[plan/10 §8](../../plan/10-任务状态与恢复设计.md)）：它是业务提示，不是技术失败。
    """
    if job.status == "completed":
        print(f"任务 {job.job_id} 已完成（{job.kind}）")
        return 0
    print(f"任务 {job.job_id} 到达终态但未成功：{job.status}", file=sys.stderr)
    if job.error:
        print(
            f"  [{job.error.get('code', '-')}] {job.error.get('message', '')}",
            file=sys.stderr,
        )
    if job.result:
        print(f"  任务结果：{json.dumps(job.result, ensure_ascii=False)}", file=sys.stderr)
    return EXIT_JOB_UNSUCCESSFUL


def _wait_for_job(database, lock, job_id: str, *, interval: float) -> int:
    """等待**本命令自己入队的那一个** ``job_id`` 到达终态。

    存活判断一律走 `WorkerLock.probe()`（临时加锁后立即退回），**不读
    `last_activity_at`**：[plan/10 §4](../../plan/10-任务状态与恢复设计.md) 禁止用时间戳猜进程存活——时间戳只能说明
    「上次动过」，进程被杀后锁会由操作系统释放，但时间戳会永远停在那一刻。
    """
    print(f"等待任务 {job_id}（每 {interval:g} 秒查看一次；Ctrl+C 可中断，任务不会被取消）")
    while True:
        job = _read_job(database, job_id)
        if job is None:  # pragma: no cover - 本进程刚写过这一行
            print(f"任务 {job_id} 不在库里，无法等待", file=sys.stderr)
            return 1
        if job.status in _TERMINAL_JOB_STATUSES:
            return _report_job_outcome(job)
        if lock.probe():
            # 入队后 worker 退出了：继续等只会挂住，直接以 4 退出。
            return _no_worker_message()
        time.sleep(interval)


def _print_blocked(upload_id: str, parsed: ParsedInput) -> None:
    """预检 ``blocked``：输入不满足合同，**不创建批次**（不填造标签）。"""
    report = parsed.report
    print(f"预检未通过（status={report.status}），未创建批次。", file=sys.stderr)
    for blocker in report.blockers:
        print(f"  [{blocker.code}] {blocker.message}", file=sys.stderr)
    for error in report.row_errors[:10]:
        print(f"  第 {error.source_row} 行：{error.reason}", file=sys.stderr)
    if len(report.row_errors) > 10:
        print(f"  ……另有 {len(report.row_errors) - 10} 条问题", file=sys.stderr)
    print(f"预检报告已保存：upload_id={upload_id}", file=sys.stderr)


def _run(args: argparse.Namespace) -> int:
    """``run <文件.xlsx>``：上传 → 预检 → 创建批次，然后交给 worker 执行。

    与网页走同一条服务路径（[api 规则](../../.claude/rules/api.md)）：本命令**不调用
    模型**，只把批次与任务写进库。执行模式（mock/real）由 worker 启动时决定，这里
    因此**不提供** ``--mode``——放在这里会让人以为 run 自己会调用模型。
    """
    if not args.input:
        print(
            "run 需要输入文件：python -m aidhu_om_agent run <文件.xlsx> "
            "[--sheet 名称] [--wait]",
            file=sys.stderr,
        )
        return 2
    source = Path(args.input).expanduser()
    if not source.is_file():
        print(f"找不到输入文件：{source}", file=sys.stderr)
        return 2

    config = _cli_config(args)
    if config is None:
        return 1

    from .services.batches import BatchError, create_run
    from .services.uploads import UploadStore, UploadTooLargeError

    # `--wait` 且没有 worker 时**什么都不做**：不开库、不上传、不建批次。
    lock = _worker_lock(config)
    if args.wait and lock.probe():
        return _no_worker_message()
    database = _open_database(config)

    store = UploadStore(database, config.paths.uploads)
    try:
        with source.open("rb") as handle:
            upload = store.save(source.name, handle, limits=config.limits)
    except UploadTooLargeError as exc:
        print(f"文件超过上限：{exc}", file=sys.stderr)
        return 2
    except InputReadError as exc:
        # 消息自身已说明是哪一类：工作簿不可读、或规模防护超限（S06-02 起）。
        print(f"输入读取错误：{exc}", file=sys.stderr)
        return 2

    try:
        parsed = precheck(upload.path, args.sheet, limits=config.limits)
    except InputReadError as exc:
        print(f"预检读取错误：{exc}", file=sys.stderr)
        return 2

    report = parsed.report
    if report.status != "passed":
        # 与 `inspect` 对同一个预检结果保持同一套退出码：blocked → 1。
        _print_blocked(upload.upload_id, parsed)
        return 1

    validation = store.put_validation(upload.upload_id, parsed)
    try:
        created = create_run(
            database,
            config,
            validation_id=validation.validation_id,
            idempotency_key=uuid.uuid4().hex,
            request_sha256=_digest({"validation_id": validation.validation_id}),
        )
    except BatchError as exc:
        print(f"创建批次失败：[{exc.code}] {exc.message}", file=sys.stderr)
        return 2 if exc.http_status < 500 else 1

    print(f"批次已入队：run_id={created.run_id} job_id={created.job_id}")
    print(f"  来源文件：{source.name}；工作表：{report.sheet_name or '（自动）'}")
    print(
        "  预检计数："
        f"total={report.counts.total} valid={report.counts.valid} "
        f"input_invalid={report.counts.input_invalid}"
    )
    print("  执行模式由 worker 决定；另开一个终端运行 python -m aidhu_om_agent worker")
    if args.wait:
        return _wait_for_job(database, lock, created.job_id, interval=_wait_seconds(args))
    return 0


def _resume(args: argparse.Namespace) -> int:
    """``resume <run_id>``：普通恢复（默认）或 ``--retry-failed`` 显式重试。"""
    if not args.input:
        print(
            "resume 需要批次标识：python -m aidhu_om_agent resume <run_id> "
            "[--retry-failed] [--wait]",
            file=sys.stderr,
        )
        return 2

    config = _cli_config(args)
    if config is None:
        return 1

    from .services.batches import BatchError, resume_run

    run_id = args.input.strip()
    retry_failed = bool(args.retry_failed)
    # `--wait` 且没有 worker 时**不入队**：等下去不会有结果，先让用户去起 worker。
    lock = _worker_lock(config)
    if args.wait and lock.probe():
        return _no_worker_message()
    database = _open_database(config)

    try:
        resumed = resume_run(
            database,
            run_id=run_id,
            retry_failed=retry_failed,
            idempotency_key=uuid.uuid4().hex,
            request_sha256=_digest({"retry_failed": retry_failed}),
        )
    except BatchError as exc:
        print(f"恢复失败：[{exc.code}] {exc.message}", file=sys.stderr)
        return 2 if exc.http_status < 500 else 1

    print(
        f"已入队恢复（retry_failed={retry_failed}）："
        f"run_id={resumed.run_id} job_id={resumed.job_id}"
    )
    print(
        f"  本轮目标记录 {resumed.selected_records} 条；"
        f"计划重开预算轮次 {resumed.renewed_campaigns}"
    )
    print(
        f"  跳过（预算用尽）{resumed.skipped_budget_exhausted} 条；"
        f"需新批次 {resumed.skipped_needs_new_batch} 条"
    )
    if resumed.finalize_only:
        print("  本次为**收尾**（零模型调用）：只把批次推到终态。")
    if resumed.dispatch_paused:
        print(
            "  注意：判别派发已暂停（model_dispatch_paused），任务排队但不会被执行。",
            file=sys.stderr,
        )
    if args.wait:
        return _wait_for_job(database, lock, resumed.job_id, interval=_wait_seconds(args))
    return 0


def _export(args: argparse.Namespace) -> int:
    """``export <run_id>``：排一份手动导出；文件由 worker 认领时生成。"""
    if not args.input:
        print(
            "export 需要批次标识：python -m aidhu_om_agent export <run_id> [--wait]",
            file=sys.stderr,
        )
        return 2

    config = _cli_config(args)
    if config is None:
        return 1

    from .services.batches import BatchError
    from .services.exports import create_manual_export

    run_id = args.input.strip()
    # `--wait` 且没有 worker 时**不入队**：空等一份永远不生成的导出没有意义。
    lock = _worker_lock(config)
    if args.wait and lock.probe():
        return _no_worker_message()
    database = _open_database(config)

    try:
        created = create_manual_export(
            database,
            run_id,
            idempotency_key=uuid.uuid4().hex,
            request_sha256=_digest({}),  # 请求体是空对象，与接口的摘要一致
        )
    except BatchError as exc:
        print(f"导出入队失败：[{exc.code}] {exc.message}", file=sys.stderr)
        return 2 if exc.http_status < 500 else 1

    print(
        f"已入队导出：run_id={run_id} export_id={created.export_id} "
        f"job_id={created.job_id}"
    )
    print("  两份文件（Excel 与 JSONL）由 worker 生成，成对发布；")
    print("  批次详情页的「导出历史」可看到状态与下载链接。")
    if args.wait:
        return _wait_for_job(database, lock, created.job_id, interval=_wait_seconds(args))
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    """``evaluate``：本阶段只定参数合同，实现属 S07。

    参数（``--run-id``、``--gold``、``--split-manifest``、``--split``）已在 ``--help``
    中可见；这里**不计算任何指标**，也不读任何文件。
    """
    print(_NOT_IMPLEMENTED, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    _enable_utf8_when_redirected()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "inspect":
        return _inspect(args)

    if args.command == "serve":
        return _serve(args)

    if args.command == "worker":
        return _worker(args)

    if args.command == "run":
        return _run(args)

    if args.command == "resume":
        return _resume(args)

    if args.command == "export":
        return _export(args)

    if args.command == "evaluate":
        return _evaluate(args)

    if args.check_config:
        return _check_config()

    print(_USAGE_HINT, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
