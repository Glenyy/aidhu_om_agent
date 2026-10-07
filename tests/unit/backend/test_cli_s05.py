"""S05-06 命令行入口的单测。

覆盖：

- ``WorkerLock.probe()`` 只探测、不写归属信息；
- ``run``：上传 → 预检 → 入队，blocked 输入不建批次；
- ``--wait``：worker 在跑 → 0；没有 worker → 4（**不留任何状态**，连库都不开）；
  任务终态未成功 → 5；等待中 worker 消失 → 4；
- ``export``：入队 → worker 生成两份文件 → 0；
- ``--config`` 优先级：显式 ``--config`` > ``config.local.toml`` > ``config.toml``；
- ``evaluate``：四个参数缺一即 2（**实现**在 S07-03，见 `test_evaluation_service.py`）。

全程模拟模式、本地 SQLite；**零真实模型调用**。``run`` 自己**永远不调用模型**——
本文件里的 worker 都由测试线程注入模拟客户端。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

import aidhu_om_agent.cli as cli
from aidhu_om_agent.config import load_config
from aidhu_om_agent.storage import Database
from aidhu_om_agent.worker import LOCK_FILENAME, Worker, WorkerLock
from fixtures.excel_samples import missing_column_workbook
from test_worker import ConfigInvalidClient, correct_factory, three_valid_workbook

#: 等待线程/状态的墙钟上限；只用于防止测试挂死，不代表被测行为。
DEADLINE_SECONDS = 30.0


def write_config(root: Path, *, name: str = "config.toml") -> Path:
    """在 ``root/configs/<name>`` 写一份可用的配置，路径全部落在 ``root`` 下。

    用绝对路径（POSIX 形式）以免落到真实项目目录：`run` 会写库、上传与产物，
    测试不能污染开发机上的 `data/` 与 `outputs/`。
    """
    configs = root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    base = root.as_posix()
    path = configs / name
    path.write_text(
        f'''[models.stage1]
base_url = ""
model = "stage1-model"
timeout_seconds = 30

[models.stage2]
base_url = ""
model = "stage2-model"
timeout_seconds = 30

[execution]
concurrency = 1
max_attempts_per_stage_campaign = 3
retry_backoff_seconds = [1, 1]

[limits]
max_upload_bytes = 4194304
max_records = 100

[paths]
database = "{base}/data/state.sqlite3"
uploads = "{base}/data/uploads"
runtime = "{base}/data/runtime"
outputs = "{base}/outputs"
logs = "{base}/logs"
frontend_dist = "{base}/dist/frontend"
''',
        encoding="utf-8",
    )
    return path


def read_rows(database_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """只读查询；库还不存在时返回空列表（调用方在等它出现）。"""
    if not database_path.is_file():
        return []
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(sql, params))
    finally:
        connection.close()


def wait_until(predicate, *, timeout: float = DEADLINE_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class BackgroundWorker:
    """在测试线程里跑一个模拟 worker，供 ``--wait`` 有真实消费者可等。"""

    def __init__(self, config_path: Path, *, client_factory=None) -> None:
        self._config = load_config(config_path)
        factory = client_factory or correct_factory()
        self.worker = Worker(
            self._config, mode="mock", poll_seconds=0.05, client_factory=factory
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stopping = threading.Event()
        self._error: BaseException | None = None

    @property
    def config(self):
        return self._config

    def _run(self) -> None:
        """一轮一轮地消费；`run_forever` 自己不检查停止标志，所以由本循环检查。"""
        try:
            while not self._stopping.is_set():
                self.worker.run_forever(max_idle_rounds=1)
        except BaseException as exc:  # pragma: no cover - 测试失败时才会看到
            self._error = exc

    def __enter__(self) -> "BackgroundWorker":
        self.worker.start()
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopping.set()
        self._thread.join(timeout=DEADLINE_SECONDS)
        self.worker.stop()
        assert not self._thread.is_alive()
        if self._error is not None:  # pragma: no cover - 只在 worker 崩了时触发
            raise AssertionError(f"worker 线程异常：{self._error!r}")


# ------------------------------------------------------------ 独占锁只探测


def test_probe_reports_the_lock_and_keeps_the_owner_stamp(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    holder = WorkerLock(path)
    assert holder.acquire(owner="w-holder") is True
    stamp = path.read_text(encoding="utf-8")
    assert "w-holder" in stamp

    prober = WorkerLock(path)
    assert prober.probe() is False  # 有 worker 在跑
    assert path.read_text(encoding="utf-8") == stamp  # 探测没有改写归属信息
    assert prober.held is False  # 也没有在本进程留下持锁状态

    holder.release()
    assert prober.probe() is True  # 锁空了：可以启动 worker
    assert path.read_text(encoding="utf-8") == stamp  # 文件仍在，内容不变


# --------------------------------------------------- run：入队与 --wait 反馈


def test_run_wait_without_a_worker_exits_4_and_leaves_no_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")

    code = cli.main(["run", str(workbook), "--config", str(config_path), "--wait"])

    assert code == cli.EXIT_NO_WORKER == 4
    assert "未检测到运行中的 worker" in capsys.readouterr().err
    # 连库都不开、文件也不上传：失败的手动等待不留下任何可误解的状态。
    assert not load_config(config_path).paths.database.exists()
    assert not (tmp_path / "data" / "uploads").exists()


def test_run_enqueues_a_batch_that_the_worker_completes(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")

    with BackgroundWorker(config_path):
        code = cli.main(["run", str(workbook), "--config", str(config_path)])

    assert code == 0
    rows = read_rows(load_config(config_path).paths.database, "SELECT status FROM runs")
    assert [row["status"] for row in rows] == ["completed"]


def test_run_wait_returns_0_when_the_job_completes(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")

    with BackgroundWorker(config_path):
        code = cli.main(
            [
                "run",
                str(workbook),
                "--config",
                str(config_path),
                "--wait",
                "--poll-seconds",
                "0.05",
            ]
        )

    out = capsys.readouterr().out
    assert code == 0
    assert "批次已入队：run_id=" in out
    assert "已完成" in out


def test_run_wait_returns_5_when_the_job_fails(tmp_path: Path, capsys) -> None:
    """系统性故障（认证类）：批次 failed，``--wait`` 如实返回 5。"""
    config_path = write_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")

    with BackgroundWorker(config_path, client_factory=ConfigInvalidClient):
        code = cli.main(
            [
                "run",
                str(workbook),
                "--config",
                str(config_path),
                "--wait",
                "--poll-seconds",
                "0.05",
            ]
        )

    err = capsys.readouterr().err
    assert code == cli.EXIT_JOB_UNSUCCESSFUL == 5
    assert "到达终态但未成功：failed" in err
    assert "CONFIG_INVALID" in err  # 错误码随任务一起报出来


def test_run_wait_returns_4_when_the_worker_goes_away(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """入队后 worker 消失：任务留着不动，等下去只会挂住，直接以 4 退出。"""
    config_path = write_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")
    config = load_config(config_path)
    lock = WorkerLock(config.paths.runtime / LOCK_FILENAME)
    assert lock.acquire(owner="w-fake") is True  # 先冒充一个在跑的 worker

    codes: list[int] = []
    runner = threading.Thread(
        target=lambda: codes.append(
            cli.main(
                [
                    "run",
                    str(workbook),
                    "--config",
                    str(config_path),
                    "--wait",
                    "--poll-seconds",
                    "0.05",
                ]
            )
        ),
        daemon=True,
    )
    try:
        runner.start()
        assert wait_until(
            lambda: read_rows(config.paths.database, "SELECT status FROM jobs")
        )
        lock.release()  # worker「退出」
        runner.join(timeout=DEADLINE_SECONDS)
    finally:
        lock.release()

    assert not runner.is_alive()
    assert codes == [cli.EXIT_NO_WORKER]
    assert "未检测到运行中的 worker" in capsys.readouterr().err
    # 任务仍然是 queued：等待失败不改队列状态。
    assert [
        row["status"]
        for row in read_rows(config.paths.database, "SELECT status FROM jobs")
    ] == ["queued"]


def test_blocked_input_does_not_create_a_batch(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    workbook = missing_column_workbook(tmp_path / "src" / "broken.xlsx")

    code = cli.main(["run", str(workbook), "--config", str(config_path)])

    assert code == 1  # 与 inspect 对同一个预检结果保持同一套退出码
    err = capsys.readouterr().err
    assert "预检未通过" in err
    assert "未创建批次" in err
    assert read_rows(load_config(config_path).paths.database, "SELECT status FROM runs") == []


# --------------------------------------------------------------- export


def test_export_enqueues_and_wait_reports_completion(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    workbook = three_valid_workbook(tmp_path / "src" / "three.xlsx")

    with BackgroundWorker(config_path):
        assert cli.main(["run", str(workbook), "--config", str(config_path)]) == 0
        run_id = read_rows(
            load_config(config_path).paths.database, "SELECT run_id FROM runs"
        )[0]["run_id"]
        code = cli.main(
            [
                "export",
                run_id,
                "--config",
                str(config_path),
                "--wait",
                "--poll-seconds",
                "0.05",
            ]
        )

    out = capsys.readouterr().out
    assert code == 0
    assert "已入队导出：run_id=" in out
    assert "任务" in out and "已完成" in out
    exports = read_rows(
        load_config(config_path).paths.database,
        "SELECT source, status, export_id FROM exports JOIN jobs USING (job_id)",
    )
    assert sorted(row["source"] for row in exports) == ["automatic", "manual"]
    # CLI 等到的就是它自己排的这份：自动导出与它先后不定（谁先入库取决于 worker
    # 何时把判别收尾），所以只断言「手动那份 completed」。
    manual = [row for row in exports if row["source"] == "manual"]
    assert [row["status"] for row in manual] == ["completed"]
    artifacts = read_rows(
        load_config(config_path).paths.database,
        "SELECT kind FROM artifacts WHERE export_id = ?",
        (manual[0]["export_id"],),
    )
    assert [row["kind"] for row in artifacts] == ["excel", "jsonl"]


def test_unknown_run_id_is_a_parameter_error(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)

    assert cli.main(["export", "does-not-exist", "--config", str(config_path)]) == 2
    assert cli.main(["resume", "does-not-exist", "--config", str(config_path)]) == 2

    err = capsys.readouterr().err
    assert "NOT_FOUND" in err
    assert "does-not-exist" in err


# ------------------------------------------------------------ --config 优先级


def test_config_precedence_explicit_then_local_then_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIDHU_PROJECT_ROOT", str(tmp_path))
    plain = write_config(tmp_path, name="config.toml")
    local = write_config(tmp_path, name="config.local.toml")
    explicit = write_config(tmp_path / "elsewhere")

    assert cli._cli_config_path(None) == local
    assert cli._cli_config_path(str(explicit)) == explicit

    args = cli._build_parser().parse_args(["run", "x.xlsx"])
    assert cli._cli_config(args).source_config == local
    args = cli._build_parser().parse_args(["run", "x.xlsx", "--config", str(explicit)])
    assert cli._cli_config(args).source_config == explicit

    local.unlink()  # 没有 config.local.toml 时回落到 config.toml
    assert cli._cli_config_path(None) == plain
    plain.unlink()  # 两份都没有时交给 load_config 给出「复制模板」的提示
    assert cli._cli_config_path(None) is None


# ----------------------------------------------------------------- evaluate


def test_evaluate_requires_all_four_arguments(capsys) -> None:
    """四个参数缺一即 2，且**在读取配置之前**就返回：不产生任何副作用。"""
    parser = cli._build_parser()
    args = parser.parse_args(
        ["evaluate", "--run-id", "r1", "--gold", "gold.xlsx", "--split", "calibration"]
    )
    assert (args.run_id, args.gold, args.split) == ("r1", "gold.xlsx", "calibration")
    assert args.split_manifest is None

    assert cli.main(["evaluate", "--run-id", "r1"]) == 2
    err = capsys.readouterr().err
    assert "--gold" in err and "--split-manifest" in err and "--split" in err


def test_no_command_prints_the_usage_hint(capsys) -> None:
    assert cli.main([]) == 2
    assert "没有指定命令" in capsys.readouterr().err


def test_open_database_migrates_only_the_configured_file(tmp_path: Path) -> None:
    """`_open_database` 建库并迁移到最新结构，且只碰配置指向的那个文件。"""
    config = load_config(write_config(tmp_path))

    database = cli._open_database(config)

    assert isinstance(database, Database)
    assert database.path == config.paths.database
    names = {
        row["name"]
        for row in read_rows(config.paths.database, "SELECT name FROM sqlite_master")
    }
    assert {"runs", "records", "exports", "artifacts", "jobs"} <= names
