"""S04-04：唯一 worker —— 独占锁、队列认领与批次执行。

依据 [plan/10 §4](../../plan/10-任务状态与恢复设计.md) 的 worker 生命周期：

1. 校验配置、路径与数据库结构版本（结构版本在 `Database.initialize()` 里检查）。
2. **获取操作系统级独占锁**；锁失败就退出，**不修改任何队列状态**。
   PID 文件只是归属元信息，不能当作锁本身。
3. 持锁后做启动恢复：上一轮遗留的 `running` 调用改为 `unknown_after_interrupt`
   （**继续占用旧预算**），`running` 任务改为 `interrupted` 并清空执行槽，
   判别任务对应的批次改为 `interrupted`；导出任务中断不改批次分类状态。
   **只有新 worker 拿到锁之后**才允许做这件事，见 [plan/10 §5]。
4. 按 `created_at, job_id` 认领 `queued` 任务，用短事务设置执行槽。
5. 判别任务按记录顺序执行，每条调用前占位尝试（`services/judging.execute_record`）。
6. 重试只由 `agent.pipeline` 的阶段执行器管理，worker 不叠加，SDK 重试保持关闭。
7. 判别结束更新 run/job、**释放执行槽**并安排自动导出；导出任务单独执行
   （导出自 S05 起才有任务可排）。认证与配置类错误不走「单条失败」，而是暂停
   判别派发并按 [plan/10 §7] 把批次标成 `failed`。
8. 无任务时短间隔轮询；退出时释放锁，**保留未消费的 queued 任务**。

不得依据 `last_activity_at` 超时抢占任务：长请求允许在配置超时内执行。
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent.diagnostics import write_record_diagnostic
from .agent.mock_samples import MockClient
from .agent.pipeline import result_to_payload
from .config import AppConfig
from .llm.client import ModelClient
from .repositories import jobs as jobs_repo
from .repositories import records as records_repo
from .repositories import runs as runs_repo
from .services.batches import BatchError
from .services.exports import execute_export, schedule_automatic_export
from .services.judging import execute_record
from .storage import Database, DatabaseError, DatabaseBusyError, utc_now, write_transaction

#: worker 模式的取值；`mock` 为默认，`real` 会产生真实调用与费用。
MODE_MOCK = "mock"
MODE_REAL = "real"
MODES: tuple[str, ...] = (MODE_MOCK, MODE_REAL)

#: 锁文件与 PID 归属文件都放在 runtime 目录下。
LOCK_FILENAME = "worker.lock"

#: 无任务时的默认轮询间隔（[plan/10 §4.8]“短间隔检查队列”）。
DEFAULT_POLL_SECONDS = 1.0

#: 导出失败信息的最长长度；错误会被发给浏览器，不把整段堆栈塞进任务行。
EXPORT_ERROR_LIMIT = 300


#: 错误文本里仍残留的绝对路径：Windows 盘符与 UNC 开头，吃到空白或引号为止。
#: 只认这两种形态，不碰 ``1/2``、``/runs`` 这类正常文本。
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"]*")


def export_error_message(exc: BaseException, outputs_root: Path) -> str:
    """导出异常 → **不含本机路径**的一行说明。

    `OSError` 的 `str()` 自带完整路径（导出目录在用户机器上的位置），而任务错误
    会被 `GET /api/runs/{run_id}/exports` 原样发给浏览器；[plan/08 §1] 要求读接口
    不发本机路径。这里把导出根目录换成 ``<outputs>``，只留相对位置。

    **必须连 `repr` 形态一起替换**：`OSError.__str__` 用 `repr()` 拼 `filename`，
    Windows 路径里的每个反斜杠在字符串里是**两个字符**；只按 `str(path)` 替换，
    在真实的 mkdir/open 失败上永远不命中（而那种失败恰恰最常带路径）。除根目录外
    再兜一层：残留的盘符/UNC 绝对路径统一换成 ``<path>``。
    """
    text = (str(exc) or type(exc).__name__).strip()
    root = str(outputs_root)
    variants = {root, root.replace("\\", "/")}
    variants |= {variant.replace("\\", "\\\\") for variant in variants}
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            # 根目录连同它后面的相对位置一起换掉：只换根会把 `\run1\exp1` 这种
            # 片段留在消息里，读起来像路径残渣。
            text = re.sub(
                re.escape(variant) + r"(?:[\\/][^\s'\"]*)?",
                "<outputs>",
                text,
                flags=re.IGNORECASE,
            )
    text = _ABSOLUTE_PATH.sub("<path>", text)
    return text.splitlines()[0][:EXPORT_ERROR_LIMIT]

#: 系统性故障错误码：认证失败、模型或必要参数无效、未预期的永久错误
#: （[plan/10 §7]）。它们会污染同一批次其余记录，因此**停止本轮消费**并把
#: 批次标成 failed，而不是让每条记录各自失败一遍。
#:
#: 反例同样重要：`CONTEXT_LIMIT`、`OUTPUT_TRUNCATED`、`OUTPUT_INVALID` 与
#: `MODEL_RETRYABLE` 都是**单条**失败，不暂停派发，其余有效行继续处理。
SYSTEMIC_FAILURE_CODES = frozenset({"CONFIG_INVALID", "MODEL_PERMANENT"})


class WorkerError(RuntimeError):
    """worker 级别的可报告错误。"""


class WorkerLockError(WorkerError):
    """独占锁被占用：**不修改队列状态**，直接退出。"""


class WorkerStateError(WorkerError):
    """执行中出现不该出现的状态（如记录数不守恒）；停止并报告。"""


class SystemicStop(WorkerError):
    """单条记录返回了系统性错误码：停止本轮、暂停派发（[plan/10 §7]）。"""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ 独占锁

#: Windows 字节范围锁**同时挡住其它句柄的读**，所以锁在文件尾部的空区域，
#: 让开头那段归属信息在 worker 运行期间仍然可以用记事本打开（人工排错要用）。
#: 偏移超过 EOF 也能锁（Windows 允许），无需把文件撑大。
_LOCK_OFFSET = 4096


def _lock_fd(fd: int) -> None:
    """非阻塞地对 ``fd`` 加独占锁；已被占用时抛 ``OSError``。"""
    if os.name == "nt":  # pragma: no cover - 平台分支
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - 平台分支
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - 平台分支
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - 平台分支
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class WorkerLock:
    """操作系统级独占锁。

    **不是 PID 文件**：进程被杀后操作系统自动释放锁，而残留的 PID 文件会让
    「进程是否还活着」变成一个猜谜游戏。锁文件里写的归属信息只供人看。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, owner: str) -> bool:
        """尝试加锁；成功返回 ``True``，已被占用返回 ``False``（不抛异常）。"""
        if self._fd is not None:
            raise WorkerError("本进程已持有锁，不要重复获取")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self._write_owner(owner)
        return True

    def probe(self) -> bool:
        """只探测锁是否空闲：临时加锁后立刻退回，**不写归属信息**。

        与 `acquire` 的区别是它不改变锁文件内容、不在本进程留下持锁状态，因此可以
        被 CLI 的 ``--wait`` 反复调用。判断「有没有 worker 在跑」依据的是操作系统
        锁本身——进程被杀后锁自动释放，不会像时间戳那样把死进程判成活的
        （[plan/10 §4] 禁止按 `last_activity_at` 猜进程存活）。

        返回 ``False`` 只说明**当下**拿不到锁（有 worker 持有，或本进程已持有）。
        """
        if self._fd is not None:
            # 本进程已持有：此时「空闲」这个判断没有意义，一律当作不可探测。
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            return False
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)
        return True

    def release(self) -> None:
        """释放锁；**不删除锁文件**（删除会让并发等待者锁到不同的 inode）。"""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)

    def _write_owner(self, owner: str) -> None:
        fd = self._fd
        if fd is None:  # pragma: no cover - 内部不变量
            return
        stamp = f"worker_id={owner} pid={os.getpid()} since={_now_iso()}\n"
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, stamp.encode("utf-8"))
        os.ftruncate(fd, len(stamp.encode("utf-8")))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 结果类型


@dataclass(frozen=True)
class JobOutcome:
    """一次任务执行的结局；供 CLI 与测试核对，不进入接口合同。

    ``counts`` 对判别任务是记录状态计数，对导出任务是**捕获时**的计数
    （两者同一来源：`services.batches.counts_of`）。``export_id`` 只在导出任务上出现。
    """

    job_id: str
    run_id: str
    status: str
    counts: dict[str, int]
    records_attempted: int
    kind: str = jobs_repo.CLASSIFY_KIND
    export_id: str | None = None


class Worker:
    """单进程串行的判别 worker。

    ``client_factory`` 在测试里可注入假客户端；生产用按模式构造：
    模拟模式逐条构造 `MockClient`（它绑定记录），真实模式共用一个
    `OpenAICompatibleClient`。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        mode: str = MODE_MOCK,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        client_factory: Callable[[Any], ModelClient] | None = None,
    ) -> None:
        if mode not in MODES:
            raise WorkerError(f"未知 worker 模式 {mode!r}；只接受 {'、'.join(MODES)}")
        self._config = config
        self._mode = mode
        self._poll_seconds = max(float(poll_seconds), 0.05)
        self._sleep = sleep
        self._client_factory = client_factory
        self._database = Database(config.paths.database)
        self._lock = WorkerLock(config.paths.runtime / LOCK_FILENAME)
        self._real_client: ModelClient | None = None
        self.worker_id = f"w-{uuid.uuid4().hex[:12]}"
        self.recovery: jobs_repo.RecoveryReport | None = None

    @property
    def mode(self) -> str:
        return self._mode

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> jobs_repo.RecoveryReport:
        """拿锁 → 迁移/版本检查 → 中断恢复 → 登记运行时状态。

        锁被占用时抛 `WorkerLockError`，**在此之前不碰数据库**，因此不会改到
        队列状态（[plan/10 §4.2]）。
        """
        if not self._lock.acquire(owner=self.worker_id):
            raise WorkerLockError(
                f"已有 worker 持有独占锁（{self._lock.path}）；"
                "同一时刻只允许一个 worker 消费队列"
            )
        try:
            self._database.initialize()
            connection = self._database.connect()
            try:
                with write_transaction(connection) as tx:
                    self.recovery = jobs_repo.recover_interrupted_state(
                        tx, now=utc_now()
                    )
                    jobs_repo.record_worker_start(
                        tx,
                        worker_id=self.worker_id,
                        mode=self._mode,
                        now=utc_now(),
                        clear_pause=True,
                    )
            finally:
                connection.close()
        except BaseException:
            # 启动失败必须放锁，否则这台机器再也起不了 worker。
            self._lock.release()
            raise
        return self.recovery

    def stop(self) -> None:
        """释放锁；**保留未消费的 queued 任务**，不做任何队列改写。"""
        self._lock.release()

    def __enter__(self) -> "Worker":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ---------------------------------------------------------------- 主循环

    def run_once(self) -> JobOutcome | None:
        """认领并执行一个任务；没有可认领的任务时返回 ``None``。

        系统性故障（存储层故障或模型调用层面的认证/配置错误）都走
        `_fail_systemically`：暂停判别、保留队列、如实标记失败。

        **导出任务走另一条收尾路径**：它失败只标任务自己，见 `_execute_export`。
        """
        job = self._claim()
        if job is None:
            return None
        if job.kind == jobs_repo.EXPORT_KIND:
            return self._execute_export(job)
        try:
            return self._execute(job)
        except (DatabaseError, WorkerStateError) as exc:
            code = (
                "STORAGE_UNAVAILABLE"
                if isinstance(exc, DatabaseError)
                else "INTERNAL_ERROR"
            )
            self._fail_systemically(job, code=code, message=str(exc))
            return None
        except SystemicStop as stop:
            self._fail_systemically(job, code=stop.code, message=stop.message)
            return None
        except BatchError as exc:
            # 批次本身执行不下去（快照/版本不可用、记录状态与检查点冲突）。这是
            # 系统性阻断：暂停并在界面上说清楚，不猜测、也不把它降级成单条失败。
            self._fail_systemically(job, code=exc.code, message=exc.message)
            return None

    def run_forever(self, *, max_idle_rounds: int | None = None) -> int:
        """持续消费队列；``max_idle_rounds`` 次空轮询后返回（供 `--once` 使用）。"""
        idle = 0
        processed = 0
        while True:
            outcome = self.run_once()
            if outcome is None:
                idle += 1
                if max_idle_rounds is not None and idle >= max_idle_rounds:
                    return processed
                self._sleep(self._poll_seconds)
                continue
            idle = 0
            processed += 1

    # ---------------------------------------------------------------- 认领

    def _claim(self) -> jobs_repo.JobRow | None:
        """短事务里认领一个 queued 任务；暂停判别时**只认领导出任务**。

        闸门管的是模型派发，不是队列：`model_dispatch_paused` 时判别任务保留在
        队列里等修好，已经排上的导出照常跑完（[plan/10 §4.7]）。
        """
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                state = jobs_repo.get_runtime_state(tx)
                kinds = (
                    (jobs_repo.EXPORT_KIND,)
                    if state.model_dispatch_paused
                    else jobs_repo.JOB_KINDS
                )
                return jobs_repo.claim_next_job(
                    tx, worker_id=self.worker_id, now=utc_now(), kinds=kinds
                )
        except DatabaseBusyError:
            # 并发写入只是暂时冲突；下一轮再试，不判为系统性故障。
            return None
        finally:
            connection.close()

    # ---------------------------------------------------------------- 执行

    def _execute(self, job: jobs_repo.JobRow) -> JobOutcome:
        run_id = job.run_id
        record_keys = [str(key) for key in job.payload.get("record_keys", ())]
        attempted = 0

        self._apply_reopen(job)

        for record_key in record_keys:
            record = self._load_record(record_key)
            if record is None or record.run_id != run_id:
                raise WorkerStateError(
                    f"任务 {job.job_id} 的记录 {record_key} 不在批次 {run_id} 中"
                )
            if record.status in ("input_invalid", "completed"):
                # 输入失败行不调用模型；已完成的记录普通恢复不重复调用。
                continue
            self._touch(job.job_id, record_key)
            execution = execute_record(
                self._database,
                self._config,
                run_id=run_id,
                record_key=record_key,
                job_id=job.job_id,
                client=self._client_for(record),
                sleep=self._sleep,
            )
            attempted += 1
            self._write_diagnostic(job, record, execution)

            # 认证/配置类错误会让后续每条记录重复失败，还可能是真实费用。停下来
            # 报告，不把「同一次故障」重复记到其余记录上（[plan/10 §7]）。
            failure = execution.result.failure
            if failure is not None and failure.code in SYSTEMIC_FAILURE_CODES:
                raise SystemicStop(
                    code=failure.code,
                    message=f"记录 {record_key} 在{failure.stage}返回系统性错误："
                    f"{failure.message}",
                )

        return self._finalize(job, attempted=attempted)

    def _execute_export(self, job: jobs_repo.JobRow) -> JobOutcome | None:
        """执行一个导出任务：捕获快照 → 写两份文件 → 成对登记。

        导出失败**只标任务自己**：不写 `runs`、不设判别闸门、不清空任何分类结果。
        导出是判别之后的一条独立支路（[plan/10 §7—§8]），导出没做成不该把用户
        已经拿到的标签作废；反过来，判别暂停也不该挡着导出。
        """
        try:
            outcome = execute_export(
                self._database, job=job, outputs_root=self._config.paths.outputs
            )
        except Exception as exc:  # noqa: BLE001 - 见下方说明
            # 导出可能因为磁盘、快照不自洽或存储冲突失败，这些都不该冒泡成
            # 「系统性故障」去改批次状态。如实记在任务上，让界面显示失败原因。
            self._fail_export(job, exc)
            return None
        return JobOutcome(
            job_id=job.job_id,
            run_id=job.run_id,
            status=jobs_repo.JOB_COMPLETED,
            counts=dict(outcome.counts),
            records_attempted=0,
            kind=jobs_repo.EXPORT_KIND,
            export_id=outcome.export_id,
        )

    def _fail_export(self, job: jobs_repo.JobRow, exc: BaseException) -> None:
        """把导出任务置为失败，**不触碰批次**。

        错误信息里的导出根目录换成 ``<outputs>``：这条 message 会被
        `GET /api/runs/{run_id}/exports` 原样发给浏览器，而 OSError 的 `str()`
        自带完整本机路径；[plan/08 §1] 要求读接口不发本机路径。
        """
        error = {
            "code": "EXPORT_FAILED",
            "message": export_error_message(exc, self._config.paths.outputs),
            "error_type": type(exc).__name__,
        }
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                jobs_repo.finish_job(
                    tx,
                    job_id=job.job_id,
                    status=jobs_repo.JOB_FAILED,
                    finished_at=utc_now(),
                    error=error,
                )
        finally:
            connection.close()

    def _apply_reopen(self, job: jobs_repo.JobRow) -> int:
        """把任务里的**重开计划**落实成新的预算轮次；返回实际重开数。

        重开计划在入队时只记录（`services.batches.resume_run`），在这里才真正
        `open_campaign`：用户点了恢复但 worker 没起来时，不会留下已经花掉一轮预算
        的记录（[plan/10 §6]）。

        幂等靠 `created_by_job_id`：同一任务的第二条重开请求看到最新轮次已经由
        自己建立，就跳过。这样「重开完成、任务在执行中被中断、再次恢复同一任务」
        不会把轮次一路推高（S04 阶段文档 §3 完成条件）。
        """
        raw = job.payload.get("reopen") or {}
        if not raw:
            return 0

        max_attempts = self._config.execution.max_attempts_per_stage_campaign
        opened = 0
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                for record_key in sorted(raw):
                    for number in raw[record_key]:
                        stage = int(number)
                        latest = records_repo.latest_campaign(tx, str(record_key), stage)
                        if (
                            latest is not None
                            and latest.created_by_job_id == job.job_id
                            and latest.reason == records_repo.CAMPAIGN_EXPLICIT_RETRY
                        ):
                            continue
                        records_repo.open_campaign(
                            tx,
                            record_key=str(record_key),
                            stage=stage,
                            max_attempts=max_attempts,
                            reason=records_repo.CAMPAIGN_EXPLICIT_RETRY,
                            created_at=utc_now(),
                            job_id=job.job_id,
                        )
                        opened += 1
        finally:
            connection.close()
        return opened

    def _write_diagnostic(
        self, job: jobs_repo.JobRow, record: records_repo.RecordRow, execution: Any
    ) -> None:
        """把**值得判读**的记录执行落到 `<runtime>/judge-diagnostics/`。

        迁自 S03 的临时接口路径（S06 阶段文档 §0.2 第 6 项），判定口径不变；写盘失败
        只记日志，**绝不改变任务状态**（`write_record_diagnostic` 内部整段兜住）。
        """
        write_record_diagnostic(
            runtime_root=self._config.paths.runtime,
            job_id=job.job_id,
            run_id=job.run_id,
            record_key=record.record_key,
            record=record.to_qa_record(),
            payload=result_to_payload(execution.result),
            mode=self._mode,
            written_at=utc_now(),
        )

    def _load_record(self, record_key: str) -> records_repo.RecordRow | None:
        connection = self._database.connect()
        try:
            return records_repo.get_record(connection, record_key)
        finally:
            connection.close()

    def _touch(self, job_id: str, record_key: str) -> None:
        """更新可观测进度；失败不影响判定，也不改写任务状态。"""
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                jobs_repo.touch_job(
                    tx, job_id=job_id, now=utc_now(), current_record_key=record_key
                )
        finally:
            connection.close()

    def _client_for(self, record: records_repo.RecordRow) -> ModelClient:
        if self._client_factory is not None:
            return self._client_factory(record)
        if self._mode == MODE_MOCK:
            return MockClient(record.to_qa_record())
        if self._real_client is None:
            from .llm.client import OpenAICompatibleClient

            self._real_client = OpenAICompatibleClient(self._config)
        return self._real_client

    # ---------------------------------------------------------------- 收尾

    def _finalize(self, job: jobs_repo.JobRow, *, attempted: int) -> JobOutcome:
        """按记录聚合终态，更新 run/job 并释放执行槽（[plan/10 §8]）。

        §8 的汇总口径是三句话：所有记录 completed → `completed`；存在
        `input_invalid` 或 `failed` 且其余行已处理 → `partial_failed`；系统性
        错误阻止继续 → `failed`（那条路径在 `_fail_systemically`，不到这里）。
        所以这里**只有前两种结果**：即使全部有效行都技术失败，也如实记
        `partial_failed`，由显式重试处理，不替用户改写成系统性失败。

        **S06-02 起不再写 ``last_error``**（原来的 ``{code: PARTIAL_FAILED,
        failed_count}`` 是批次收尾摘要，不是「错误」）：收尾摘要由
        `failure_summary` + `counts` 承载，`last_error` 只留给真错误
        （`_fail_systemically` 那条路径继续写）。存量批次里已有的 `partial_failed`
        `last_error` **不迁移、不改写**，界面两种都能显示（S06 阶段文档 §0.3 第 8 项）。
        """
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                run = runs_repo.get_run(tx, job.run_id)
                if run is None:  # pragma: no cover - 外键保证存在
                    raise WorkerStateError(f"任务 {job.job_id} 的批次不存在")
                counts = records_repo.count_by_status(tx, run.run_id)
                remaining = counts["pending"] + counts["stage1_done"]
                if remaining:
                    raise WorkerStateError(
                        f"批次 {run.run_id} 仍有 {remaining} 条记录未处理；"
                        "不写入终态，也不猜测状态"
                    )

                status = _run_status(
                    completed=counts["completed"],
                    failed=counts["failed"],
                    valid=run.valid_count,
                    input_invalid=run.input_invalid_count,
                )
                now = utc_now()
                runs_repo.finish_run(
                    tx,
                    run_id=run.run_id,
                    status=status,
                    finished_at=now,
                    last_error=None,
                )
                jobs_repo.finish_job(
                    tx,
                    job_id=job.job_id,
                    status=status,
                    finished_at=now,
                    result={"counts": counts, "attempted": attempted},
                )
                # 终态与自动导出同一事务：界面看到 completed/partial_failed 时，
                # 导出任务已经在队列里了。`failed`（系统性故障）不排导出——
                # 那条路径在 `_fail_systemically`，且批次本身没有可导出的结论。
                export_id = schedule_automatic_export(
                    tx, run_id=run.run_id, revision=run.revision, created_at=now
                )
                return JobOutcome(
                    job_id=job.job_id,
                    run_id=run.run_id,
                    status=status,
                    counts=counts,
                    records_attempted=attempted,
                    kind=jobs_repo.CLASSIFY_KIND,
                    export_id=export_id,
                )
        finally:
            connection.close()

    # ---------------------------------------------------------------- 系统性故障

    def _fail_systemically(self, job: jobs_repo.JobRow, *, code: str, message: str) -> None:
        """系统性故障：暂停判别、保留队列、如实标记失败（[plan/10 §7]）。

        「故障事务中设置 `model_dispatch_paused`」与「run/job 进入终态」写在**同一个
        事务**里，因此不会出现「已经暂停但任务还挂在 running」的中间态。

        暂停闸门存在库里而不是进程里：崩溃重启后仍然有效。导出侧不读这个闸门，
        所以「暂停判别、导出仍可执行」自然成立。

        批次标 `failed` 时**不排自动导出**：没有可交付的分类结论，排一份导出只会
        误导（用户在修好原因后用 `POST /api/runs/{id}/exports` 手动导出）。
        """
        reason = {"code": code, "message": message, "job_id": job.job_id}
        connection = self._database.connect()
        try:
            with write_transaction(connection) as tx:
                jobs_repo.set_dispatch_paused(tx, reason=reason, now=utc_now())
                jobs_repo.finish_job(
                    tx,
                    job_id=job.job_id,
                    status=jobs_repo.JOB_FAILED,
                    finished_at=utc_now(),
                    error=reason,
                )
                runs_repo.finish_run(
                    tx,
                    run_id=job.run_id,
                    status="failed",
                    finished_at=utc_now(),
                    last_error=reason,
                )
        finally:
            connection.close()


def _run_status(
    *, completed: int, failed: int, valid: int, input_invalid: int
) -> str:
    """批次终态；见 `Worker._finalize` 的口径说明（[plan/10 §8]）。"""
    if failed == 0 and input_invalid == 0 and completed >= valid:
        return "completed"
    return "partial_failed"


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "EXPORT_ERROR_LIMIT",
    "LOCK_FILENAME",
    "MODES",
    "MODE_MOCK",
    "MODE_REAL",
    "SYSTEMIC_FAILURE_CODES",
    "JobOutcome",
    "SystemicStop",
    "Worker",
    "WorkerError",
    "WorkerLock",
    "WorkerLockError",
    "WorkerStateError",
    "export_error_message",
]
