"""S04-06 中断与多进程验证：故障注入下的状态恒等式。

按 [plan/10 §5](../../../plan/10-任务状态与恢复设计.md) 考察五个中断点：

1. 占位前——调用没有发生，预算未被占用；
2. 占位后——占位行留在 `running`，启动恢复改为 `unknown_after_interrupt` 并
   **继续占用预算**（预算不退还）；
3. 阶段一提交后——阶段一结果与检查点已提交，阶段二未占位；
4. 阶段二提交后——该记录已 completed，其余记录未处理；
5. 收尾前——所有记录已终态，批次/任务还没落终态。

外加**事务回滚**：在成功提交的中途被杀，同一事务里先执行的语句必须一并消失，
不允许出现「尝试记为成功、阶段结果却没有」的半次提交。

**故障注入方式**：`FaultyDatabase` 在连接层拦截语句，在指定时刻抛
`KillSwitch`（继承 `BaseException`，与 `KeyboardInterrupt` 同类，worker 的任何
`except` 都不会误吞）。`KillSwitch` 触发时事务被 `write_transaction` 回滚，得到的
库状态与「进程被杀时最后一次提交之后」一致——即只保留已提交的事务。

全部使用合成工作簿、本地 SQLite 与模拟客户端，**零真实模型调用**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aidhu_om_agent.repositories import jobs as jobs_repo
from aidhu_om_agent.repositories import runs as runs_repo
from aidhu_om_agent.services.batches import resume_run
from aidhu_om_agent.storage import Database
from test_batches import make_config
from test_worker import (
    Probe,
    correct_factory,
    make_worker,
    prepare_run,
    three_valid_workbook,
)


class KillSwitch(BaseException):
    """模拟进程被杀；**不是** `Exception`，以免被 worker 的错误分支吃掉。"""


class _KillingConnection:
    """真连接的代理：每条语句执行前先问一次触发器。"""

    def __init__(self, real, trigger) -> None:  # noqa: ANN001 - 测试替身
        self._real = real
        self._trigger = trigger

    def execute(self, sql, parameters=()):  # noqa: ANN001, ANN201
        self._trigger(sql)
        return self._real.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):  # noqa: ANN001, ANN201
        self._trigger(sql)
        return self._real.executemany(sql, seq_of_parameters)

    def __getattr__(self, name):  # noqa: ANN001, ANN201 - 其余属性直接透传
        return getattr(self._real, name)


class FaultyDatabase(Database):
    """在指定时刻杀死进程的数据库；除此之外与真库完全一致。"""

    def __init__(self, path: Path, *, trigger) -> None:  # noqa: ANN001
        super().__init__(path)
        self._trigger = trigger

    def connect(self):  # noqa: ANN201
        return _KillingConnection(super().connect(), self._trigger)


class OnStatement:
    """第 ``occurrence`` 次出现 ``pattern`` 时触发（在**执行该语句之前**）。"""

    def __init__(self, pattern: str, occurrence: int = 1) -> None:
        self.pattern = pattern
        self.occurrence = occurrence
        self.seen = 0

    def __call__(self, sql: str) -> None:
        if self.pattern in sql:
            self.seen += 1
            if self.seen >= self.occurrence:
                raise KillSwitch(f"命中「{self.pattern}」第 {self.seen} 次")


class AfterTransaction:
    """第 ``count`` 个写事务**提交之后**触发（即第 count+1 个 BEGIN 之前）。"""

    def __init__(self, count: int) -> None:
        self.count = count
        self.begun = 0

    def __call__(self, sql: str) -> None:
        if sql.strip().upper().startswith("BEGIN"):
            self.begun += 1
            if self.begun > self.count:
                raise KillSwitch(f"第 {self.begun} 个事务开始前被杀")


def run_until_killed(config, *, trigger, client_factory=None):  # noqa: ANN001
    """跑到被杀为止；返回 (worker, 被捕获的 KillSwitch)。

    先 `start()` 拿到锁并做启动恢复（用真库），**之后**才换上故障库：故障注入只
    覆盖消费阶段，不污染「启动」这一步本身。
    """
    worker = make_worker(config, client_factory=client_factory or correct_factory())
    worker.start()
    worker._database = FaultyDatabase(config.paths.database, trigger=trigger)  # noqa: SLF001
    try:
        with pytest.raises(KillSwitch) as caught:
            worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()
    return worker, caught.value


def restart_and_recover(config, probe: Probe, *, client_factory=None):  # noqa: ANN001
    """模拟「停 worker → 重新启动一个 worker」，返回新 worker 的恢复报告。"""
    worker = make_worker(config, client_factory=client_factory or correct_factory())
    report = worker.start()
    return worker, report


# ------------------------------------------------------------- ① 占位前中断


def test_kill_before_reservation_leaves_budget_untouched(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    _, killed = run_until_killed(config, trigger=OnStatement("INSERT INTO call_attempts"))
    assert "call_attempts" in str(killed)
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 0
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'pending'") == 3
    assert probe.scalar("SELECT status FROM jobs") == "running"

    worker, report = restart_and_recover(config, probe)
    try:
        assert report.attempts_marked_unknown == 0  # 没有占位行可标记
        assert report.jobs_marked_interrupted == 1
        assert report.runs_marked_interrupted == 1
        assert probe.scalar("SELECT status FROM jobs") == "interrupted"
        assert probe.scalar("SELECT worker_slot FROM jobs") is None
        assert probe.scalar("SELECT status FROM runs") == "interrupted"

        resumed = resume_run(database, run_id=created.run_id)
        assert (resumed.selected_records, resumed.renewed_campaigns) == (3, 0)
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    # 3 条 × 2 阶段；被杀的那次没有留下任何痕迹，也没有多花预算。
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 6
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts WHERE status = 'succeeded'") == 6


# ------------------------------------------------------------- ② 占位后中断


def test_kill_after_reservation_consumes_the_budget(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    # 占位已提交，写尝试终态的那一刻被杀：占位行留在 running。
    trigger = OnStatement("UPDATE call_attempts SET status")
    run_until_killed(config, trigger=trigger)
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 1
    assert probe.scalar("SELECT status FROM call_attempts") == "running"
    assert probe.scalar("SELECT status FROM records ORDER BY order_index LIMIT 1") == "pending"

    worker, report = restart_and_recover(config, probe)
    try:
        assert report.attempts_marked_unknown == 1
        assert probe.scalar("SELECT status FROM call_attempts") == "unknown_after_interrupt"
        assert probe.scalar("SELECT status FROM runs") == "interrupted"

        resumed = resume_run(database, run_id=created.run_id)
        # 普通恢复：旧轮次还剩 2 次，够用，因此**不重开**预算。
        assert (resumed.selected_records, resumed.renewed_campaigns) == (3, 0)
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar(
        "SELECT COUNT(*) FROM stage_campaigns WHERE reason = 'explicit_retry'"
    ) == 0
    # 被中断的那次**仍然占着**预算：第 1 条记录阶段一用了 2 次（未知 + 成功）。
    first_key = probe.scalar("SELECT record_key FROM records ORDER BY order_index LIMIT 1")
    attempts = probe.all(
        "SELECT a.status FROM call_attempts a JOIN stage_campaigns c"
        " ON c.campaign_id = a.campaign_id WHERE c.record_key = ? AND c.stage = 1"
        " ORDER BY a.attempt_no",
        (first_key,),
    )
    assert [row["status"] for row in attempts] == ["unknown_after_interrupt", "succeeded"]
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 7


# -------------------------------------------------------- ③ 阶段一提交后中断


def test_kill_after_stage1_commit_resumes_only_stage2(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    # 第 2 次占位 = 阶段二的占位；在此之前被杀，阶段一已经提交。
    run_until_killed(config, trigger=OnStatement("INSERT INTO call_attempts", 2))

    assert probe.scalar("SELECT COUNT(*) FROM stage_results WHERE stage = 1") == 1
    assert probe.scalar("SELECT COUNT(*) FROM stage_results WHERE stage = 2") == 0
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 1
    assert probe.scalar("SELECT status FROM call_attempts") == "succeeded"
    assert probe.scalar("SELECT status FROM records ORDER BY order_index LIMIT 1") == "stage1_done"

    worker, report = restart_and_recover(config, probe)
    try:
        # 阶段一的尝试已回报终态，没有 running 占位需要标记。
        assert report.attempts_marked_unknown == 0
        resumed = resume_run(database, run_id=created.run_id)
        assert resumed.selected_records == 3  # 3 条都还有事可做
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    first_key = probe.scalar("SELECT record_key FROM records ORDER BY order_index LIMIT 1")
    # 阶段一**没有重跑**：仍然只有那 1 次成功尝试。
    assert probe.scalar(
        "SELECT COUNT(*) FROM call_attempts a JOIN stage_campaigns c"
        " ON c.campaign_id = a.campaign_id WHERE c.record_key = ? AND c.stage = 1",
        (first_key,),
    ) == 1
    assert probe.scalar("SELECT COUNT(*) FROM stage_results") == 6


# -------------------------------------------------------- ④ 阶段二提交后中断


def test_kill_after_stage2_commit_keeps_the_finished_record(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    # 事务序号：1=认领；每条记录 5 个（touch、阶段一占位、阶段一提交、阶段二占位、
    # 阶段二提交）；再往后是下一条记录的 touch 与最终收尾。第 6 个事务提交后被杀，
    # 正落在「第 1 条记录阶段二已提交、第 2 条记录还没开始」的位置。
    run_until_killed(config, trigger=AfterTransaction(6))

    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'completed'") == 1
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'pending'") == 2
    assert probe.scalar("SELECT status FROM runs") == "running"
    assert probe.scalar("SELECT finished_at FROM runs") is None

    worker, report = restart_and_recover(config, probe)
    try:
        assert report.jobs_marked_interrupted == 1
        assert report.runs_marked_interrupted == 1
        assert report.attempts_marked_unknown == 0

        resumed = resume_run(database, run_id=created.run_id)
        assert resumed.selected_records == 2  # 已完成的那条不在目标里
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'completed'") == 3
    # 已完成的记录没被再调用一次：总共仍是 3 × 2 次尝试。
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 6
    assert probe.scalar("SELECT COUNT(*) FROM stage_results") == 6


# ------------------------------------------------------------- ⑤ 收尾前中断


def test_kill_before_finalize_needs_only_finalization(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    # 事务序号：1=认领 + 每条记录 5 个（touch、阶段一占位、阶段一提交、阶段二占位、
    # 阶段二提交），3 条记录共 16 个；第 17 个才是 `_finalize`。
    run_until_killed(config, trigger=AfterTransaction(16))

    assert probe.scalar("SELECT COUNT(*) FROM records WHERE status = 'completed'") == 3
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 6
    assert probe.scalar("SELECT status FROM runs") == "running"
    assert probe.scalar("SELECT finished_at FROM runs") is None

    worker, report = restart_and_recover(config, probe)
    try:
        assert report.jobs_marked_interrupted == 1 and report.runs_marked_interrupted == 1
        assert report.attempts_marked_unknown == 0

        resumed = resume_run(database, run_id=created.run_id)
        assert resumed.finalize_only is True
        assert resumed.selected_records == 0 and resumed.renewed_campaigns == 0

        calls: list[str] = []
        worker._client_factory = correct_factory(calls)  # noqa: SLF001 - 核对零调用
        worker.run_forever(max_idle_rounds=1)
        assert calls == []  # 仅收尾：**一次模型调用都没有**
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT finished_at FROM runs") is not None
    assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 6


# --------------------------------------------------------------- 事务回滚


def test_kill_mid_transaction_rolls_the_whole_commit_back(tmp_path: Path) -> None:
    """成功提交是**一次事务**：中途被杀不能留下「尝试成功、结果没有」的半次提交。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    # `_commit_stage` 的顺序是 尝试终态 → 阶段结果 → 记录 → revision；
    # 在写阶段结果之前被杀，前面那条「尝试成功」必须一起回滚。
    run_until_killed(config, trigger=OnStatement("INSERT INTO stage_results"))

    assert probe.scalar("SELECT COUNT(*) FROM stage_results") == 0
    assert probe.scalar("SELECT status FROM call_attempts") == "running"
    assert probe.scalar("SELECT status FROM records ORDER BY order_index LIMIT 1") == "pending"
    assert probe.scalar("SELECT revision FROM runs WHERE run_id = ?", (created.run_id,)) == 0

    # 回滚之后仍能正常恢复：占位行按中断处理，预算照数。
    worker, report = restart_and_recover(config, probe)
    try:
        assert report.attempts_marked_unknown == 1
        resume_run(database, run_id=created.run_id)
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    assert probe.scalar("SELECT status FROM runs") == "completed"
    assert probe.scalar("SELECT COUNT(*) FROM stage_results") == 6


def test_killed_worker_still_holds_the_lock_for_another_one(tmp_path: Path) -> None:
    """被杀但没退出的 worker 仍然持锁：第二个 worker 拒绝启动，且不改队列状态。"""
    config = make_config(tmp_path)
    database, created = prepare_run(
        config, three_valid_workbook(tmp_path / "src" / "three.xlsx"), tmp_path
    )
    probe = Probe(database)

    worker = make_worker(config)
    worker.start()
    worker._database = FaultyDatabase(  # noqa: SLF001
        config.paths.database, trigger=OnStatement("INSERT INTO call_attempts")
    )
    try:
        with pytest.raises(KillSwitch):
            worker.run_forever(max_idle_rounds=1)
        assert probe.scalar("SELECT status FROM jobs") == "running"
        assert probe.scalar("SELECT COUNT(*) FROM call_attempts") == 0

        from aidhu_om_agent.worker import WorkerLockError

        second = make_worker(config)
        with pytest.raises(WorkerLockError):
            second.start()
        # 第二个 worker 连库都没碰：任务还是 running，没有第二个 worker 归属。
        assert probe.scalar("SELECT status FROM jobs") == "running"
        assert probe.scalar("SELECT status FROM runs") == "running"
    finally:
        worker.stop()

    # 前一个退出后，新 worker 才能拿到锁并做中断恢复。
    worker2, report = restart_and_recover(config, probe)
    try:
        assert report.jobs_marked_interrupted == 1
        assert probe.scalar("SELECT worker_slot FROM jobs") is None
    finally:
        worker2.stop()


__all__ = ["jobs_repo", "runs_repo"]
