"""批次记录的读写（S04-02）。

记录的两种键不能混用（[plan/08 §1](../../../plan/08-API接口与数据合同.md)）：

- ``record_id``：**业务编号**，来自工作表，可能缺失，也可能含不适合放进 URL 的字符。
- ``record_key``：**内部主键**，程序生成的 UUID，批次内唯一，用于查询与恢复。

因此 ``record_key`` 不是编号的变形；缺编号的行照常有 ``record_key``。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from ..schemas.qa import (
    A_COLUMN,
    ID_COLUMN,
    INPUT_COLUMNS,
    Q_COLUMN,
    REF_FIELDS,
    QARecord,
)

#: 输入失败行的可读原因存在 `input_error_json`；不是业务标签。
RecordStatus = str


def new_record_key() -> str:
    return uuid.uuid4().hex


def refs_to_json(refs: dict[str, str | None]) -> str:
    """按 ref1—ref10 固定顺序序列化；空资料写作空字符串（plan/09 §3）。"""
    return json.dumps(
        {field: (refs.get(field) or "") for field in REF_FIELDS},
        ensure_ascii=False,
        sort_keys=False,
    )


def refs_from_json(text: str) -> dict[str, str | None]:
    """反序列化并还原「空资料 = ``None``」：模型与提示词按 `None` 判空。"""
    raw = json.loads(text)
    return {field: (raw.get(field) or None) for field in REF_FIELDS}


def raw_input_of(record: QARecord) -> dict[str, str | None]:
    """13 个输入字段的可读值，仅作来源留痕（plan/09 §3）。"""
    values: dict[str, str | None] = {
        ID_COLUMN: record.record_id,
        Q_COLUMN: record.q,
        A_COLUMN: record.a,
    }
    for field in REF_FIELDS:
        values[field] = record.refs.get(field)
    return {column: values.get(column) for column in INPUT_COLUMNS}


@dataclass(frozen=True)
class NewRecord:
    """待插入的一条记录。"""

    record_key: str
    record_id: str | None
    source_row: int
    order_index: int
    q: str | None
    a: str | None
    refs: dict[str, str | None]
    raw_input: dict[str, str | None]
    status: str
    input_error: dict[str, Any] | None = None


def new_record_from_qa(
    record: QARecord, *, input_error: dict[str, Any] | None = None
) -> NewRecord:
    """把解析结果转成待插入记录；``input_error`` 非空即输入失败行。"""
    return NewRecord(
        record_key=new_record_key(),
        record_id=record.record_id,
        source_row=record.source_row,
        order_index=record.order_index,
        q=record.q,
        a=record.a,
        refs=dict(record.refs),
        raw_input=raw_input_of(record),
        status="input_invalid" if input_error else "pending",
        input_error=input_error,
    )


@dataclass(frozen=True)
class RecordRow:
    """`records` 的一行。"""

    record_key: str
    run_id: str
    record_id: str | None
    source_row: int
    order_index: int
    q: str | None
    a: str | None
    refs: dict[str, str | None]
    raw_input: dict[str, str | None]
    input_error: dict[str, Any] | None
    status: str
    final_label: str | None
    review_required: int | None
    failure_stage: str | None
    failure: dict[str, Any] | None
    created_at: str
    updated_at: str

    def to_qa_record(self) -> QARecord:
        """还原成判别用的记录对象（输入失败行还原后仍缺 q/a）。"""
        return QARecord(
            record_id=self.record_id,
            source_row=self.source_row,
            order_index=self.order_index,
            q=self.q,
            a=self.a,
            refs=dict(self.refs),
        )


def _row_to_record(row: sqlite3.Row) -> RecordRow:
    return RecordRow(
        record_key=row["record_key"],
        run_id=row["run_id"],
        record_id=row["record_id"],
        source_row=int(row["source_row"]),
        order_index=int(row["order_index"]),
        q=row["q"],
        a=row["a"],
        refs=refs_from_json(row["refs_json"]),
        raw_input=dict(json.loads(row["raw_input_json"])),
        input_error=json.loads(row["input_error_json"]) if row["input_error_json"] else None,
        status=row["status"],
        final_label=row["final_label"],
        review_required=(
            int(row["review_required"]) if row["review_required"] is not None else None
        ),
        failure_stage=row["failure_stage"],
        failure=json.loads(row["failure_json"]) if row["failure_json"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def insert_records(
    connection: sqlite3.Connection,
    run_id: str,
    records: tuple[NewRecord, ...],
    *,
    created_at: str,
) -> None:
    """批量插入记录；与批次创建同一事务（调用方负责事务边界）。"""
    connection.executemany(
        "INSERT INTO records (record_key, run_id, record_id, source_row, order_index,"
        " q, a, refs_json, raw_input_json, input_error_json, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.record_key,
                run_id,
                item.record_id,
                item.source_row,
                item.order_index,
                item.q,
                item.a,
                refs_to_json(item.refs),
                json.dumps(item.raw_input, ensure_ascii=False),
                json.dumps(item.input_error, ensure_ascii=False) if item.input_error else None,
                item.status,
                created_at,
                created_at,
            )
            for item in records
        ],
    )


def list_records(connection: sqlite3.Connection, run_id: str) -> tuple[RecordRow, ...]:
    """按输入顺序返回批次全部记录（含 input_invalid 行）。"""
    return tuple(
        _row_to_record(row)
        for row in connection.execute(
            "SELECT * FROM records WHERE run_id = ? ORDER BY order_index", (run_id,)
        )
    )


def get_record(connection: sqlite3.Connection, record_key: str) -> RecordRow | None:
    row = connection.execute(
        "SELECT * FROM records WHERE record_key = ?", (record_key,)
    ).fetchone()
    return None if row is None else _row_to_record(row)


def count_by_status(connection: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """按状态聚合；动态计数直接聚合 records，不另存易失步的计数列。"""
    counts = {"pending": 0, "input_invalid": 0, "stage1_done": 0, "completed": 0, "failed": 0}
    for row in connection.execute(
        "SELECT status, COUNT(*) AS n FROM records WHERE run_id = ? GROUP BY status",
        (run_id,),
    ):
        counts[str(row["status"])] = int(row["n"])
    return counts


def count_review_required(connection: sqlite3.Connection, run_id: str) -> int:
    """需要人工复核的记录数；是 `completed` 的**子集**，不加进 processed。"""
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM records WHERE run_id = ? AND review_required = 1",
        (run_id,),
    ).fetchone()
    return int(row["n"])


# --------------------------------------------- 批次详情的读取聚合（S04-07）


@dataclass(frozen=True)
class RecordFailureRow:
    """失败记录摘要；**不含证据正文**，只够界面定位到哪条、哪阶段、什么错。"""

    record_key: str
    record_id: str | None
    source_row: int
    order_index: int
    failure_stage: str | None
    failure: dict[str, Any] | None


def list_record_failures(
    connection: sqlite3.Connection, run_id: str, *, limit: int = 50
) -> tuple[RecordFailureRow, ...]:
    """按输入顺序返回失败记录；`failed` 只可能是有效记录（输入失败是另一种状态）。"""
    return tuple(
        RecordFailureRow(
            record_key=row["record_key"],
            record_id=row["record_id"],
            source_row=int(row["source_row"]),
            order_index=int(row["order_index"]),
            failure_stage=row["failure_stage"],
            failure=json.loads(row["failure_json"]) if row["failure_json"] else None,
        )
        for row in connection.execute(
            "SELECT record_key, record_id, source_row, order_index, failure_stage,"
            " failure_json FROM records WHERE run_id = ? AND status = 'failed'"
            " ORDER BY order_index LIMIT ?",
            (run_id, int(limit)),
        )
    )


def count_record_failures(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM records WHERE run_id = ? AND status = 'failed'",
        (run_id,),
    ).fetchone()
    return int(row["n"])


def attempt_statistics(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, dict[str, int]]:
    """按阶段统计尝试：总数、各终态、其中有多少次是模拟调用。

    界面用它核对**恢复没有重复调用阶段一**：恢复前后阶段一的 `attempts` 不变，
    只有阶段二的数字增长，就说明检查点被复用而不是整条重跑（S04-07 界面清单）。
    ``unknown_after_interrupt`` 与 ``running`` 都算已占用预算，因此都在总数里。
    """
    def _empty() -> dict[str, int]:
        return {
            "attempts": 0,
            "succeeded": 0,
            "failed": 0,
            "unknown_after_interrupt": 0,
            "simulated": 0,
        }

    stats: dict[str, dict[str, int]] = {"stage1": _empty(), "stage2": _empty()}
    for row in connection.execute(
        "SELECT c.stage AS stage, a.status AS status, a.simulated AS simulated,"
        " COUNT(*) AS n FROM call_attempts a"
        " JOIN stage_campaigns c ON c.campaign_id = a.campaign_id"
        " JOIN records r ON r.record_key = c.record_key"
        " WHERE r.run_id = ? GROUP BY c.stage, a.status, a.simulated",
        (run_id,),
    ):
        bucket = stats[stage_name(int(row["stage"]))]
        count = int(row["n"])
        bucket["attempts"] += count
        if row["status"] in bucket:
            bucket[row["status"]] += count
        if row["simulated"]:
            bucket["simulated"] += count
    return stats


# ------------------------------------------------- 阶段预算与提交（S04-03）

#: plan/10 §3：每阶段每轮 campaign 最多 3 次尝试；数据库 CHECK 同值。
MAX_CAMPAIGN_ATTEMPTS = 3

CAMPAIGN_INITIAL = "initial"
CAMPAIGN_EXPLICIT_RETRY = "explicit_retry"

#: 尝试终态；`running` 只出现在「已占位、结果未提交」的窗口里。
ATTEMPT_RUNNING = "running"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"
ATTEMPT_UNKNOWN = "unknown_after_interrupt"

_STAGE_NUMBERS = {"stage1": 1, "stage2": 2}


def stage_number(stage: str) -> int:
    """`"stage1"`/`"stage2"` → 1/2；数据库用整数列存阶段。"""
    try:
        return _STAGE_NUMBERS[stage]
    except KeyError:
        raise ValueError(f"未知阶段 {stage!r}；只接受 stage1、stage2") from None


def stage_name(number: int) -> str:
    """1/2 → `"stage1"`/`"stage2"`。"""
    for name, value in _STAGE_NUMBERS.items():
        if value == number:
            return name
    raise ValueError(f"未知阶段序号 {number!r}")


@dataclass(frozen=True)
class CampaignRow:
    """`stage_campaigns` 的一行；一轮预算的范围。

    预算**不随普通恢复清零**：继续用最新一轮剩余的尝试次数。只有显式重试才新开
    一轮（``campaign_no`` 递增，上限仍是 `max_attempts`）。
    """

    campaign_id: str
    record_key: str
    stage: int
    campaign_no: int
    max_attempts: int
    reason: str
    created_by_job_id: str | None
    created_at: str


@dataclass(frozen=True)
class AttemptRow:
    """`call_attempts` 的一行；``final_content`` 是模型最终正文，不是推理链。"""

    attempt_id: str
    campaign_id: str
    job_id: str | None
    attempt_no: int
    status: str
    requested_model_id: str | None
    returned_model_id: str | None
    request_digest: str | None
    final_content: str | None
    usage: dict[str, Any] | None
    duration_ms: int | None
    service_request_id: str | None
    error: dict[str, Any] | None
    simulated: bool
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class StageResultRow:
    """`stage_results` 的一行；每记录每阶段最多一条已校验结果。"""

    stage_result_id: str
    record_key: str
    stage: int
    attempt_id: str
    result_json: str
    schema_version: str
    prompt_sha256: str
    result_sha256: str
    validated_at: str


def _row_to_campaign(row: sqlite3.Row) -> CampaignRow:
    return CampaignRow(
        campaign_id=row["campaign_id"],
        record_key=row["record_key"],
        stage=int(row["stage"]),
        campaign_no=int(row["campaign_no"]),
        max_attempts=int(row["max_attempts"]),
        reason=row["reason"],
        created_by_job_id=row["created_by_job_id"],
        created_at=row["created_at"],
    )


def _row_to_attempt(row: sqlite3.Row) -> AttemptRow:
    return AttemptRow(
        attempt_id=row["attempt_id"],
        campaign_id=row["campaign_id"],
        job_id=row["job_id"],
        attempt_no=int(row["attempt_no"]),
        status=row["status"],
        requested_model_id=row["requested_model_id"],
        returned_model_id=row["returned_model_id"],
        request_digest=row["request_digest"],
        final_content=row["final_content"],
        usage=json.loads(row["usage_json"]) if row["usage_json"] else None,
        duration_ms=(int(row["duration_ms"]) if row["duration_ms"] is not None else None),
        service_request_id=row["service_request_id"],
        error=json.loads(row["error_json"]) if row["error_json"] else None,
        simulated=bool(row["simulated"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def latest_campaign(
    connection: sqlite3.Connection, record_key: str, stage: int
) -> CampaignRow | None:
    """该记录该阶段最新一轮 campaign；没有则为 ``None``（预算还未占用）。"""
    row = connection.execute(
        "SELECT * FROM stage_campaigns WHERE record_key = ? AND stage = ?"
        " ORDER BY campaign_no DESC LIMIT 1",
        (record_key, int(stage)),
    ).fetchone()
    return None if row is None else _row_to_campaign(row)


def open_campaign(
    connection: sqlite3.Connection,
    *,
    record_key: str,
    stage: int,
    max_attempts: int,
    reason: str,
    created_at: str,
    job_id: str | None = None,
) -> CampaignRow:
    """新开一轮预算；``campaign_no`` 在已有轮次上加一。

    ``reason='initial'`` 用于首次执行，``'explicit_retry'`` 只由显式重试入口使用
    （[plan/10 §6]）；普通恢复**不得**调用它来「重置成 3」。
    """
    if reason not in (CAMPAIGN_INITIAL, CAMPAIGN_EXPLICIT_RETRY):
        raise ValueError(f"未知预算重开原因 {reason!r}")
    if not 1 <= int(max_attempts) <= MAX_CAMPAIGN_ATTEMPTS:
        raise ValueError(
            f"每阶段每轮尝试上限必须在 1—{MAX_CAMPAIGN_ATTEMPTS} 之间，收到 {max_attempts!r}"
        )
    row = connection.execute(
        "SELECT COALESCE(MAX(campaign_no), 0) AS n FROM stage_campaigns"
        " WHERE record_key = ? AND stage = ?",
        (record_key, int(stage)),
    ).fetchone()
    campaign = CampaignRow(
        campaign_id=uuid.uuid4().hex,
        record_key=record_key,
        stage=int(stage),
        campaign_no=int(row["n"]) + 1,
        max_attempts=int(max_attempts),
        reason=reason,
        created_by_job_id=job_id,
        created_at=created_at,
    )
    connection.execute(
        "INSERT INTO stage_campaigns (campaign_id, record_key, stage, campaign_no,"
        " max_attempts, reason, created_by_job_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            campaign.campaign_id,
            campaign.record_key,
            campaign.stage,
            campaign.campaign_no,
            campaign.max_attempts,
            campaign.reason,
            campaign.created_by_job_id,
            campaign.created_at,
        ),
    )
    return campaign


def count_campaign_attempts(connection: sqlite3.Connection, campaign_id: str) -> int:
    """该轮已占用的尝试次数——**含失败与中断未知的尝试**，预算不退还。"""
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM call_attempts WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()
    return int(row["n"])


def reserve_attempt(
    connection: sqlite3.Connection,
    *,
    campaign_id: str,
    attempt_no: int,
    started_at: str,
    job_id: str | None = None,
    requested_model_id: str | None = None,
) -> str:
    """登记一次尝试占位；**必须在模型调用之前提交**。

    行以 `running` 落库：进程若在调用中被杀，这行就是「远端结果未知」的证据，
    由启动恢复改为 `unknown_after_interrupt` 并继续占用预算。
    """
    attempt_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO call_attempts (attempt_id, campaign_id, job_id, attempt_no, status,"
        " requested_model_id, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            campaign_id,
            job_id,
            int(attempt_no),
            ATTEMPT_RUNNING,
            requested_model_id,
            started_at,
        ),
    )
    return attempt_id


def finish_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    status: str,
    finished_at: str,
    returned_model_id: str | None = None,
    usage: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    error: dict[str, Any] | None = None,
    content: str | None = None,
    simulated: bool = False,
) -> None:
    """写入尝试终态；``content`` 按 plan/09 §4 落最终响应正文（不含推理链）。

    ``simulated`` 只对模拟客户端的调用置 1：模拟结果必须在数据层可分辨，界面才能
    显著标识（[.claude/rules/review-and-handoff.md §4.3]）。
    """
    if status not in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN):
        raise ValueError(f"未知尝试终态 {status!r}")
    connection.execute(
        "UPDATE call_attempts SET status = ?, returned_model_id = ?, usage_json = ?,"
        " duration_ms = ?, error_json = ?, final_content = ?, simulated = ?, finished_at = ?"
        " WHERE attempt_id = ?",
        (
            status,
            returned_model_id,
            json.dumps(usage, ensure_ascii=False) if usage is not None else None,
            int(duration_ms) if duration_ms is not None else None,
            json.dumps(error, ensure_ascii=False) if error is not None else None,
            content,
            1 if simulated else 0,
            finished_at,
            attempt_id,
        ),
    )


def attempt_by_no(
    connection: sqlite3.Connection, campaign_id: str, attempt_no: int
) -> AttemptRow | None:
    row = connection.execute(
        "SELECT * FROM call_attempts WHERE campaign_id = ? AND attempt_no = ?",
        (campaign_id, int(attempt_no)),
    ).fetchone()
    return None if row is None else _row_to_attempt(row)


def list_attempts(
    connection: sqlite3.Connection, record_key: str
) -> tuple[AttemptRow, ...]:
    """按阶段与尝试序号返回该记录的全部尝试（含历史轮次）。"""
    return tuple(
        _row_to_attempt(row)
        for row in connection.execute(
            "SELECT a.* FROM call_attempts a JOIN stage_campaigns c"
            " ON a.campaign_id = c.campaign_id WHERE c.record_key = ?"
            " ORDER BY c.stage, c.campaign_no, a.attempt_no",
            (record_key,),
        )
    )


def list_attempts_by_stage(
    connection: sqlite3.Connection, record_key: str
) -> dict[int, tuple[AttemptRow, ...]]:
    """按阶段分组的全部尝试（含历史轮次）；组内按轮次、尝试序号排序。

    导出要按阶段分列计数（S05-02 的 `attempt_summary`）：`list_attempts` 返回的
    扁平序列里没有阶段号，硬从排序推阶段是脆的，所以这里让 SQL 把 `c.stage`
    一起取出来。
    """
    grouped: dict[int, list[AttemptRow]] = {}
    for row in connection.execute(
        "SELECT a.*, c.stage AS campaign_stage FROM call_attempts a"
        " JOIN stage_campaigns c ON a.campaign_id = c.campaign_id"
        " WHERE c.record_key = ? ORDER BY c.stage, c.campaign_no, a.attempt_no",
        (record_key,),
    ):
        grouped.setdefault(int(row["campaign_stage"]), []).append(_row_to_attempt(row))
    return {stage: tuple(items) for stage, items in sorted(grouped.items())}


def insert_stage_result(
    connection: sqlite3.Connection,
    *,
    record_key: str,
    stage: int,
    attempt_id: str,
    result_json: str,
    schema_version: str,
    prompt_sha256: str,
    validated_at: str,
) -> str:
    """写一条已校验的阶段结果；与记录状态、批次 revision **同一事务**。"""
    stage_result_id = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO stage_results (stage_result_id, record_key, stage, attempt_id,"
        " result_json, schema_version, prompt_sha256, result_sha256, validated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stage_result_id,
            record_key,
            int(stage),
            attempt_id,
            result_json,
            schema_version,
            prompt_sha256,
            hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
            validated_at,
        ),
    )
    return stage_result_id


def get_stage_result(
    connection: sqlite3.Connection, record_key: str, stage: int
) -> StageResultRow | None:
    row = connection.execute(
        "SELECT * FROM stage_results WHERE record_key = ? AND stage = ?",
        (record_key, int(stage)),
    ).fetchone()
    if row is None:
        return None
    return StageResultRow(
        stage_result_id=row["stage_result_id"],
        record_key=row["record_key"],
        stage=int(row["stage"]),
        attempt_id=row["attempt_id"],
        result_json=row["result_json"],
        schema_version=row["schema_version"],
        prompt_sha256=row["prompt_sha256"],
        result_sha256=row["result_sha256"],
        validated_at=row["validated_at"],
    )


# ------------------------------------------------------- 记录状态与投影更新


def mark_stage1_done(
    connection: sqlite3.Connection, record_key: str, *, updated_at: str
) -> None:
    """阶段一提交后的记录状态；标签与复核标记仍为空（数据库 CHECK 同值）。"""
    connection.execute(
        "UPDATE records SET status = 'stage1_done', final_label = NULL,"
        " review_required = NULL, failure_stage = NULL, failure_json = NULL,"
        " updated_at = ? WHERE record_key = ?",
        (updated_at, record_key),
    )


def mark_completed(
    connection: sqlite3.Connection,
    record_key: str,
    *,
    final_label: str,
    review_required: bool,
    updated_at: str,
) -> None:
    """阶段二提交后的记录状态与投影（唯一允许出现标签的状态）。"""
    connection.execute(
        "UPDATE records SET status = 'completed', final_label = ?, review_required = ?,"
        " failure_stage = NULL, failure_json = NULL, updated_at = ? WHERE record_key = ?",
        (final_label, 1 if review_required else 0, updated_at, record_key),
    )


def mark_failed(
    connection: sqlite3.Connection,
    record_key: str,
    *,
    failure_stage: int,
    failure: dict[str, Any],
    updated_at: str,
) -> None:
    """技术失败：**不给标签**，只留失败阶段与受控错误。"""
    connection.execute(
        "UPDATE records SET status = 'failed', final_label = NULL, review_required = NULL,"
        " failure_stage = ?, failure_json = ?, updated_at = ? WHERE record_key = ?",
        (stage_name(int(failure_stage)), json.dumps(failure, ensure_ascii=False), updated_at, record_key),
    )


def set_checkpoint_status(
    connection: sqlite3.Connection,
    record_key: str,
    status: str,
    *,
    updated_at: str,
) -> None:
    """把**失败**行按已提交的阶段结果重置回检查点状态。

    只在 worker 认领待执行记录时调用（[plan/10 §3]：不在用户点击恢复时提前擦掉
    错误）；合法目标是 `pending`（无阶段一结果）与 `stage1_done`（有阶段一结果）。
    """
    if status not in ("pending", "stage1_done"):
        raise ValueError(f"检查点状态只能是 pending 或 stage1_done，收到 {status!r}")
    connection.execute(
        "UPDATE records SET status = ?, final_label = NULL, review_required = NULL,"
        " failure_stage = NULL, failure_json = NULL, updated_at = ? WHERE record_key = ?",
        (status, updated_at, record_key),
    )


__all__ = [
    "ATTEMPT_FAILED",
    "ATTEMPT_RUNNING",
    "ATTEMPT_SUCCEEDED",
    "ATTEMPT_UNKNOWN",
    "CAMPAIGN_EXPLICIT_RETRY",
    "CAMPAIGN_INITIAL",
    "MAX_CAMPAIGN_ATTEMPTS",
    "AttemptRow",
    "CampaignRow",
    "NewRecord",
    "RecordFailureRow",
    "RecordRow",
    "StageResultRow",
    "attempt_by_no",
    "attempt_statistics",
    "count_by_status",
    "count_campaign_attempts",
    "count_record_failures",
    "count_review_required",
    "finish_attempt",
    "get_record",
    "get_stage_result",
    "insert_records",
    "insert_stage_result",
    "latest_campaign",
    "list_attempts",
    "list_attempts_by_stage",
    "list_record_failures",
    "list_records",
    "mark_completed",
    "mark_failed",
    "mark_stage1_done",
    "new_record_from_qa",
    "new_record_key",
    "open_campaign",
    "raw_input_of",
    "refs_from_json",
    "refs_to_json",
    "reserve_attempt",
    "set_checkpoint_status",
    "stage_name",
    "stage_number",
]
