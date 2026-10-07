"""任务队列、导出登记与幂等键的读写（S04-02；认领与执行槽在 S04-04）。

任务只描述「要做什么」，不保存业务结果：判别结果落在 `records`/`stage_results`，
任务行只留结果标识与受控错误。**API 只入队，长模型调用由 worker 执行**
（见 [.claude/rules/api.md]）。

一个批次最多一个活跃判别任务是**数据库层**保证（`uq_active_classification`
部分唯一索引），服务层不靠「先查后写」维持该规则。

幂等键与资源**同一事务**提交（[plan/08 §4]）：同键同请求体返回原资源，
同键异请求体 409，失败操作不留记录。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

#: 任务状态；与 `runs` 共用同一套取值。
JobStatus = str

ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")


def new_job_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class JobRow:
    """`jobs` 的一行。"""

    job_id: str
    run_id: str
    kind: str
    mode: str
    payload: dict[str, Any]
    status: str
    worker_id: str | None
    worker_slot: int | None
    current_record_key: str | None
    current_stage: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: str
    started_at: str | None
    last_activity_at: str | None
    finished_at: str | None


def _row_to_job(row: sqlite3.Row) -> JobRow:
    return JobRow(
        job_id=row["job_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        mode=row["mode"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        worker_id=row["worker_id"],
        worker_slot=(
            int(row["worker_slot"]) if row["worker_slot"] is not None else None
        ),
        current_record_key=row["current_record_key"],
        current_stage=row["current_stage"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=json.loads(row["error_json"]) if row["error_json"] else None,
        created_at=row["created_at"],
        started_at=row["started_at"],
        last_activity_at=row["last_activity_at"],
        finished_at=row["finished_at"],
    )


def insert_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    run_id: str,
    kind: str,
    mode: str,
    payload: dict[str, Any],
    created_at: str,
    status: str = "queued",
) -> None:
    """入队一个任务；与批次创建同一事务，避免出现「有批次没有任务」。"""
    connection.execute(
        "INSERT INTO jobs (job_id, run_id, kind, mode, payload_json, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, run_id, kind, mode, json.dumps(payload, ensure_ascii=False), status, created_at),
    )


def get_job(connection: sqlite3.Connection, job_id: str) -> JobRow | None:
    row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return None if row is None else _row_to_job(row)


def find_active_classification(
    connection: sqlite3.Connection, run_id: str
) -> JobRow | None:
    """该批次当前的活跃判别任务；没有则为 ``None``。"""
    row = connection.execute(
        "SELECT * FROM jobs WHERE run_id = ? AND kind = 'classify'"
        " AND status IN ('queued', 'running')"
        " ORDER BY created_at, job_id LIMIT 1",
        (run_id,),
    ).fetchone()
    return None if row is None else _row_to_job(row)


def list_jobs_for_run(connection: sqlite3.Connection, run_id: str) -> tuple[JobRow, ...]:
    return tuple(
        _row_to_job(row)
        for row in connection.execute(
            "SELECT * FROM jobs WHERE run_id = ? ORDER BY created_at, job_id", (run_id,)
        )
    )


# ------------------------------------------------ 队列认领与执行槽（S04-04）

#: 执行槽的固定取值；唯一索引 `uq_worker_slot` 保证同一时刻只有一个 running 任务。
WORKER_SLOT = 1

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_PARTIAL_FAILED = "partial_failed"
JOB_FAILED = "failed"
JOB_INTERRUPTED = "interrupted"

TERMINAL_JOB_STATUSES: tuple[str, ...] = (
    JOB_COMPLETED,
    JOB_PARTIAL_FAILED,
    JOB_FAILED,
    JOB_INTERRUPTED,
)


def claim_next_job(
    connection: sqlite3.Connection, *, worker_id: str, now: str
) -> JobRow | None:
    """认领最老的 queued 判别任务；没有可认领的任务时返回 ``None``。

    顺序固定为 `created_at, job_id`（[plan/10 §4.4]）。判别任务在执行槽被占用时
    **不等待**：唯一索引 `uq_worker_slot` 会拒绝第二次认领，本函数把
    `IntegrityError` 当作「暂时没有可认领的任务」，不修改任何队列状态。
    """
    row = connection.execute(
        "SELECT * FROM jobs WHERE status = 'queued' AND kind = 'classify'"
        " ORDER BY created_at, job_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None

    try:
        connection.execute(
            "UPDATE jobs SET status = 'running', worker_id = ?, worker_slot = ?,"
            " started_at = COALESCE(started_at, ?), last_activity_at = ? WHERE job_id = ?",
            (worker_id, WORKER_SLOT, now, now, row["job_id"]),
        )
    except sqlite3.IntegrityError:
        return None

    connection.execute(
        "UPDATE runs SET status = 'running', started_at = COALESCE(started_at, ?)"
        " WHERE run_id = ? AND status = 'queued'",
        (now, row["run_id"]),
    )
    claimed = get_job(connection, row["job_id"])
    return claimed


def touch_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    now: str,
    current_record_key: str | None = None,
    current_stage: str | None = None,
) -> None:
    """更新任务的**可观测进度**。

    `last_activity_at` 只用于展示；[plan/10 §4] 明确**不得**依据它超时抢占任务，
    长请求允许在配置超时内执行。
    """
    connection.execute(
        "UPDATE jobs SET last_activity_at = ?, current_record_key = ?, current_stage = ?"
        " WHERE job_id = ?",
        (now, current_record_key, current_stage, job_id),
    )


def finish_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    status: str,
    finished_at: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """任务进入终态并**释放执行槽**；释放后下一个任务才能被认领。"""
    if status not in TERMINAL_JOB_STATUSES:
        raise ValueError(f"未知任务终态 {status!r}")
    connection.execute(
        "UPDATE jobs SET status = ?, worker_slot = NULL, current_record_key = NULL,"
        " current_stage = NULL, result_json = ?, error_json = ?,"
        " last_activity_at = ?, finished_at = ? WHERE job_id = ?",
        (
            status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            json.dumps(error, ensure_ascii=False) if error is not None else None,
            finished_at,
            finished_at,
            job_id,
        ),
    )


def count_jobs_by_status(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {
        JOB_QUEUED: 0,
        JOB_RUNNING: 0,
        JOB_COMPLETED: 0,
        JOB_PARTIAL_FAILED: 0,
        JOB_FAILED: 0,
        JOB_INTERRUPTED: 0,
    }
    for row in connection.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ):
        counts[str(row["status"])] = int(row["n"])
    return counts


# ------------------------------------------------- 中断恢复与运行时状态（S04-04）


@dataclass(frozen=True)
class RecoveryReport:
    """一次启动恢复的结果；全部是计数，不含业务判断。"""

    attempts_marked_unknown: int
    jobs_marked_interrupted: int
    runs_marked_interrupted: int

    @property
    def changed(self) -> bool:
        return bool(
            self.attempts_marked_unknown
            or self.jobs_marked_interrupted
            or self.runs_marked_interrupted
        )


def recover_interrupted_state(
    connection: sqlite3.Connection, *, now: str
) -> RecoveryReport:
    """把上一轮遗留的 running 状态登记为中断；**只在新 worker 拿到锁之后调用**。

    依据 [plan/10 §4—§5]：

    - `running` 的调用改为 `unknown_after_interrupt`，并**继续占用旧预算**
      （行本身不删，`attempts_used` 照数）；
    - `running` 的任务改为 `interrupted` 并清空执行槽；
    - 判别任务中断时对应批次改为 `interrupted`；导出任务中断不改批次分类状态。
    """
    attempts = connection.execute(
        "UPDATE call_attempts SET status = 'unknown_after_interrupt', finished_at = ?"
        " WHERE status = 'running'",
        (now,),
    ).rowcount

    interrupted = list(
        connection.execute(
            "SELECT job_id, run_id, kind FROM jobs WHERE status = 'running'"
        ).fetchall()
    )
    for row in interrupted:
        connection.execute(
            "UPDATE jobs SET status = 'interrupted', worker_slot = NULL,"
            " current_record_key = NULL, current_stage = NULL, finished_at = ?"
            " WHERE job_id = ?",
            (now, row["job_id"]),
        )
    classification_runs = {
        str(row["run_id"]) for row in interrupted if row["kind"] == "classify"
    }
    for run_id in sorted(classification_runs):
        connection.execute(
            "UPDATE runs SET status = 'interrupted' WHERE run_id = ? AND status = 'running'",
            (run_id,),
        )

    return RecoveryReport(
        attempts_marked_unknown=int(attempts or 0),
        jobs_marked_interrupted=len(interrupted),
        runs_marked_interrupted=len(classification_runs),
    )


@dataclass(frozen=True)
class RuntimeState:
    """`runtime_state` 的单行；worker 归属与判别闸门。"""

    worker_id: str | None
    worker_started_at: str | None
    worker_mode: str | None
    model_dispatch_paused: bool
    pause_reason: dict[str, Any] | None
    updated_at: str | None


def get_runtime_state(connection: sqlite3.Connection) -> RuntimeState:
    row = connection.execute(
        "SELECT * FROM runtime_state WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        return RuntimeState(None, None, None, False, None, None)
    return RuntimeState(
        worker_id=row["worker_id"],
        worker_started_at=row["worker_started_at"],
        worker_mode=row["worker_mode"],
        model_dispatch_paused=bool(row["model_dispatch_paused"]),
        pause_reason=(
            json.loads(row["pause_reason_json"]) if row["pause_reason_json"] else None
        ),
        updated_at=row["updated_at"],
    )


def record_worker_start(
    connection: sqlite3.Connection,
    *,
    worker_id: str,
    mode: str,
    now: str,
    clear_pause: bool,
) -> None:
    """登记本次 worker 并（配置检查通过后）解除上一轮的暂停。

    暂停状态存在库里而不是进程里，因此崩溃后仍会阻止判别继续消费队列。
    """
    if mode not in ("mock", "real"):
        raise ValueError(f"未知 worker 模式 {mode!r}")
    connection.execute(
        "INSERT INTO runtime_state (singleton_id, worker_id, worker_started_at,"
        " worker_mode, model_dispatch_paused, pause_reason_json, updated_at)"
        " VALUES (1, ?, ?, ?, 0, NULL, ?)"
        " ON CONFLICT (singleton_id) DO UPDATE SET worker_id = excluded.worker_id,"
        " worker_started_at = excluded.worker_started_at,"
        " worker_mode = excluded.worker_mode, updated_at = excluded.updated_at,"
        " model_dispatch_paused = CASE WHEN ? THEN 0 ELSE model_dispatch_paused END,"
        " pause_reason_json = CASE WHEN ? THEN NULL ELSE pause_reason_json END",
        (worker_id, now, mode, now, 1 if clear_pause else 0, 1 if clear_pause else 0),
    )


def set_dispatch_paused(
    connection: sqlite3.Connection, *, reason: dict[str, Any], now: str
) -> None:
    """暂停判别派发；**已排队任务保留**，导出不受影响（[plan/10 §4.7]）。"""
    connection.execute(
        "INSERT INTO runtime_state (singleton_id, model_dispatch_paused,"
        " pause_reason_json, updated_at) VALUES (1, 1, ?, ?)"
        " ON CONFLICT (singleton_id) DO UPDATE SET model_dispatch_paused = 1,"
        " pause_reason_json = excluded.pause_reason_json, updated_at = excluded.updated_at",
        (json.dumps(reason, ensure_ascii=False), now),
    )


__all__ = [
    "ACTIVE_STATUSES",
    "IdempotencyRow",
    "JobRow",
    "JobStatus",
    "JOB_COMPLETED",
    "JOB_FAILED",
    "JOB_INTERRUPTED",
    "JOB_PARTIAL_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "RecoveryReport",
    "RuntimeState",
    "TERMINAL_JOB_STATUSES",
    "WORKER_SLOT",
    "claim_next_job",
    "count_jobs_by_status",
    "find_active_classification",
    "finish_job",
    "get_idempotent",
    "get_job",
    "get_runtime_state",
    "insert_job",
    "list_jobs_for_run",
    "new_job_id",
    "put_idempotent",
    "record_worker_start",
    "recover_interrupted_state",
    "set_dispatch_paused",
    "touch_job",
]


@dataclass(frozen=True)
class IdempotencyRow:
    """一次已成功完成的写请求；响应体原样保存以便重放。"""

    scope: str
    key: str
    request_sha256: str
    http_status: int
    response_data: dict[str, Any]
    run_id: str | None
    job_id: str | None
    created_at: str


def get_idempotent(
    connection: sqlite3.Connection, *, scope: str, key: str
) -> IdempotencyRow | None:
    row = connection.execute(
        "SELECT * FROM idempotency_keys WHERE scope = ? AND key = ?", (scope, key)
    ).fetchone()
    if row is None:
        return None
    return IdempotencyRow(
        scope=row["scope"],
        key=row["key"],
        request_sha256=row["request_sha256"],
        http_status=int(row["http_status"]),
        response_data=json.loads(row["response_data_json"]),
        run_id=row["run_id"],
        job_id=row["job_id"],
        created_at=row["created_at"],
    )


def put_idempotent(
    connection: sqlite3.Connection,
    *,
    scope: str,
    key: str,
    request_sha256: str,
    http_status: int,
    response_data: dict[str, Any],
    created_at: str,
    run_id: str | None = None,
    job_id: str | None = None,
) -> None:
    """登记一次成功响应；由调用方放在与资源写入同一个事务里。"""
    connection.execute(
        "INSERT INTO idempotency_keys (scope, key, request_sha256, http_status,"
        " response_data_json, run_id, job_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope,
            key,
            request_sha256,
            int(http_status),
            json.dumps(response_data, ensure_ascii=False),
            run_id,
            job_id,
            created_at,
        ),
    )
