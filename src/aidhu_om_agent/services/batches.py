"""批次创建与输入快照核对（S04-02）。

`POST /api/runs` 只收 ``validation_id``：模型、地址、超时与提示词都由后端从配置与
代码冻结进快照，浏览器不提交（[plan/08 §4](../../../plan/08-API接口与数据合同.md)）。

**输入快照怎么核对**（S04 阶段文档 §0.4 ① 的落地方式）：预检报告是**报告级**快照，
记录行按 plan/09 §3 属于批次，因此创建时不「从 JSON 反序列化记录行」，而是：

1. 重新计算原文件摘要，必须与预检时一致，否则 409 `INPUT_SNAPSHOT_CHANGED`；
2. 用**同一个原文件**重新解析（文件未变 ⇒ 输入未变），
3. 把重算出的报告与持久化快照逐字段比对，不一致即 409 `VERSION_INCOMPATIBLE`
   （说明解析实现变了而合同版本没变，属于必须人工介入的情况）。

比"从 JSON 重建"更强：既验证快照，又验证解析实现，且不引入第二份记录级快照。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from ..agent.pipeline import CODE_OUTPUT_INVALID, RAW_OUTPUT_ELLIPSIS
from ..config import AppConfig
from ..excel.reader import InputReadError, file_digest, precheck
from ..repositories import jobs as jobs_repo
from ..repositories import records as records_repo
from ..repositories import runs as runs_repo
from ..schemas.judgement import LABELS
from ..schemas.qa import INPUT_CONTRACT_VERSION, ParsedInput, PrecheckReport
from ..storage import (
    Database,
    DatabaseBusyError,
    read_transaction,
    utc_now,
    write_transaction,
)
from ..version import prompt_snapshot, versions_snapshot

#: 创建批次的幂等范围；键为「方法 + 标准化路径」。
RUN_CREATE_SCOPE = "POST /api/runs"


def _resume_scope(run_id: str) -> str:
    """恢复的幂等范围；按批次区分，避免不同批次互相顶掉幂等键。"""
    return f"POST /api/runs/{run_id}/resume"


#: 这些失败在**同批次**里重试没有意义：要么改限制、要么改输入（[plan/10 §6]
#: 「不可在同批次修正的问题」）。它们不计入 `skipped_budget_exhausted`——那个
#: 数字是「有剩余预算就能救」的行数，混进来会让人以为重开预算就能修好。
NEW_BATCH_REQUIRED_CODES = frozenset({"CONTEXT_LIMIT", "OUTPUT_TRUNCATED"})

#: 可以恢复的批次状态：这些状态下的记录才可能还有事可做。
RESUMABLE_RUN_STATUSES = frozenset(
    {"queued", "running", "partial_failed", "failed", "interrupted"}
)

#: 需要「仅收尾」的批次状态：记录都已终态，只差把 run/job 落到终态。
FINALIZE_ONLY_RUN_STATUSES = frozenset({"queued", "running", "interrupted"})


class BatchError(Exception):
    """创建/恢复批次的业务拒绝；``code`` 用 plan/08 的错误码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


@dataclass(frozen=True)
class RunCreation:
    """一次创建的结果；``reused`` 为真表示命中了幂等键，没有新建任何东西。"""

    run_id: str
    job_id: str
    reused: bool = False
    http_status: int = 202


def load_parsed_input(
    connection: sqlite3.Connection,
    validation_id: str,
    *,
    config: AppConfig,
) -> ParsedInput:
    """重算并核对某个预检的记录；文件变了或解析实现变了都拒绝。"""
    validation = runs_repo.get_input_validation(connection, validation_id)
    if validation is None:
        raise BatchError(
            "NOT_FOUND", f"未知 validation_id：{validation_id}；请重新预检", http_status=404
        )
    upload = runs_repo.get_upload(connection, validation.upload_id)
    if upload is None:  # pragma: no cover - 外键保证存在
        raise BatchError("NOT_FOUND", f"上传记录缺失：{validation.upload_id}", http_status=404)

    if validation.input_contract_version != INPUT_CONTRACT_VERSION:
        raise BatchError(
            "VERSION_INCOMPATIBLE",
            f"预检使用的输入合同版本 {validation.input_contract_version} 与当前 "
            f"{INPUT_CONTRACT_VERSION} 不一致；请重新预检",
            details={"validation_contract": validation.input_contract_version},
        )

    path = upload.path(config.paths.uploads)
    if not path.is_file():
        raise BatchError("STORAGE_UNAVAILABLE", f"原始文件已不在：{path.name}", http_status=503)

    if file_digest(path) != validation.file_sha256:
        raise BatchError(
            "INPUT_SNAPSHOT_CHANGED",
            "原始文件内容与预检时不一致；请重新上传并预检",
            details={"validation_id": validation_id},
        )

    try:
        parsed = precheck(path, validation.sheet_name, limits=config.limits)
    except InputReadError as exc:
        raise BatchError("BAD_WORKBOOK", str(exc), http_status=422) from exc

    if _report_digest(parsed.report) != _report_digest(validation.report):
        raise BatchError(
            "VERSION_INCOMPATIBLE",
            "同一份文件重算出的预检结果与快照不一致；解析实现可能已变化，请重新预检",
            details={"validation_id": validation_id},
        )
    return parsed


def _report_digest(report: PrecheckReport) -> dict[str, Any]:
    """报告的规范化形式；用于比对而不受字段顺序影响。"""
    return report.model_dump(mode="json")


def _record_input_errors(parsed: ParsedInput) -> dict[int, dict[str, Any]]:
    """来源行 → 输入失败原因；与 plan/09 的 `input_error_json` 对应。"""
    return {
        error.source_row: {"reason": error.reason, "record_id": error.record_id}
        for error in parsed.report.row_errors
    }


def create_run(
    database: Database,
    config: AppConfig,
    *,
    validation_id: str,
    idempotency_key: str | None = None,
    request_sha256: str | None = None,
) -> RunCreation:
    """从一次 `passed` 预检创建批次与排队任务；失败不留半个批次。

    幂等、批次、记录与任务**同一事务**提交（[plan/08 §4]）。
    """
    connection = database.connect()
    try:
        if idempotency_key:
            existing = jobs_repo.get_idempotent(
                connection, scope=RUN_CREATE_SCOPE, key=idempotency_key
            )
            if existing is not None:
                if existing.request_sha256 != (request_sha256 or ""):
                    raise BatchError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 Idempotency-Key 已用于不同的请求体",
                        details={"key": idempotency_key},
                    )
                return RunCreation(
                    run_id=str(existing.response_data["run_id"]),
                    job_id=str(existing.response_data["job_id"]),
                    reused=True,
                    http_status=existing.http_status,
                )

        validation = runs_repo.get_input_validation(connection, validation_id)
        if validation is None:
            raise BatchError(
                "NOT_FOUND",
                f"未知 validation_id：{validation_id}；请重新预检",
                http_status=404,
            )
        if validation.status != "passed":
            raise BatchError(
                "INPUT_NOT_VALIDATED",
                "该预检为 blocked，不能创建批次；请修正输入后重新预检",
                details={"status": validation.status},
            )

        # 创建时按批次为维度检查活跃任务是不可能的（run 还不存在）：同一预检用新的
        # 幂等键可以有意再建一个批次（plan/08 §4「不禁止有意重新运行」），而同一批次
        # 的再次执行走 resume。并发下由 uq_active_classification 兜底，见下面的 except。
        parsed = load_parsed_input(connection, validation_id, config=config)
        upload = runs_repo.get_upload(connection, validation.upload_id)
        if upload is None:  # pragma: no cover - load_parsed_input 已确认
            raise BatchError("NOT_FOUND", f"上传记录缺失：{validation.upload_id}", http_status=404)
        if parsed.report.sheet_name is None:  # pragma: no cover - passed 必有工作表
            raise BatchError("BAD_WORKBOOK", "预检未确定工作表，不能创建批次", http_status=422)

        run_id = uuid.uuid4().hex
        job_id = jobs_repo.new_job_id()
        created_at = utc_now()
        errors = _record_input_errors(parsed)
        new_records = tuple(
            records_repo.new_record_from_qa(record, input_error=errors.get(record.source_row))
            for record in parsed.records
        )
        response_data = {"run_id": run_id, "job_id": job_id}

        try:
            with write_transaction(connection) as conn:
                runs_repo.insert_run(
                    conn,
                    run_id=run_id,
                    upload_id=upload.upload_id,
                    validation_id=validation_id,
                    source_filename=upload.original_filename,
                    sheet_name=parsed.report.sheet_name,
                    input_digest=upload.sha256,
                    config_snapshot=config.snapshot(),
                    prompt_snapshot=prompt_snapshot(),
                    versions=versions_snapshot(),
                    report=parsed.report,
                    status="queued",
                    created_at=created_at,
                )
                records_repo.insert_records(conn, run_id, new_records, created_at=created_at)
                jobs_repo.insert_job(
                    conn,
                    job_id=job_id,
                    run_id=run_id,
                    kind="classify",
                    mode="initial",
                    payload={"record_keys": [item.record_key for item in new_records]},
                    created_at=created_at,
                )
                if idempotency_key:
                    jobs_repo.put_idempotent(
                        conn,
                        scope=RUN_CREATE_SCOPE,
                        key=idempotency_key,
                        request_sha256=request_sha256 or "",
                        http_status=202,
                        response_data=response_data,
                        created_at=created_at,
                        run_id=run_id,
                        job_id=job_id,
                    )
        except sqlite3.IntegrityError as exc:
            # 唯一索引 uq_active_classification 在并发下兜底；服务层也做了前置检查。
            raise BatchError(
                "STATE_CONFLICT", "该批次已有排队或运行中的判别任务", details={"reason": str(exc)}
            ) from exc

        return RunCreation(run_id=run_id, job_id=job_id)
    except DatabaseBusyError as exc:
        raise BatchError("DB_BUSY", str(exc), http_status=503) from exc
    finally:
        connection.close()


# ------------------------------------------------- 普通恢复与显式重试（S04-05）


@dataclass(frozen=True)
class ResumePlan:
    """一次恢复的可选记录与重开计划；**只描述，不改状态**。

    `selected_records` 是 worker 本轮要处理的目标；`reopen` 里每个元素是
    ``(record_key, 阶段号)``，表示该阶段要**新开一轮预算**（[plan/10 §6]）。
    """

    run_id: str
    run_status: str
    retry_failed: bool
    selected_records: tuple[str, ...]
    reopen: tuple[tuple[str, int], ...]
    skipped_budget_exhausted: int
    skipped_needs_new_batch: int
    finalize_only: bool

    @property
    def renewed_campaigns(self) -> int:
        return len(self.reopen)

    @property
    def has_work(self) -> bool:
        return bool(self.selected_records) or self.finalize_only

    @property
    def reason_when_empty(self) -> str:
        """可选数为 0 时的**可操作**说明；不写「无记录」这种无用话。"""
        if self.skipped_needs_new_batch and not self.skipped_budget_exhausted:
            return "失败原因需要修正输入或限制，只能新建批次"
        if self.skipped_budget_exhausted and not self.skipped_needs_new_batch:
            return (
                f"{self.skipped_budget_exhausted} 条失败项的预算已用尽；"
                "需要重试失败项才能重开预算"
            )
        return "没有可恢复的记录，也没有需要收尾的状态"


def plan_resume(
    connection: sqlite3.Connection, run_id: str, *, retry_failed: bool
) -> ResumePlan:
    """算出这次恢复要处理哪些记录、哪些阶段要重开预算（[plan/10 §6]）。

    选择规则：

    - `pending`、`stage1_done`：下一阶段尚未执行，**总是可选**；
    - `failed`：失败阶段的旧 campaign **还有剩余次数**时可选（不重开）；
      次数用尽时才要求 `retry_failed=true`，并要求开新一轮预算；
    - `completed`、`input_invalid`：不纳入；
    - `CONTEXT_LIMIT`、`OUTPUT_TRUNCATED`：需要改限制或改输入，不纳入
      （算 `skipped_needs_new_batch`，且 `retry_failed` 也救不了它们）。

    「预算是否够」按**旧 campaign 自己记录的 `max_attempts`** 判断，不按当前配置：
    改配置不应让历史批次凭空多出次数（「恢复不清空预算」）。

    没有任何可选记录、且不需要收尾时由调用方转成 409 `NOTHING_TO_RESUME`。
    """
    run = runs_repo.get_run(connection, run_id)
    if run is None:
        raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)

    if jobs_repo.find_active_classification(connection, run_id) is not None:
        raise BatchError(
            "STATE_CONFLICT",
            "该批次已有排队或运行中的判别任务；等它结束后再恢复",
            details={"run_id": run_id, "status": run.status},
        )

    _check_prompt_snapshot(connection, run_id)

    selected: list[str] = []
    reopen: list[tuple[str, int]] = []
    skipped_budget = 0
    skipped_new_batch = 0
    pending_rows = 0

    for record in records_repo.list_records(connection, run_id):
        if record.status in ("completed", "input_invalid"):
            continue
        if record.status in ("pending", "stage1_done"):
            pending_rows += 1
            selected.append(record.record_key)
            continue
        if record.status != "failed":  # pragma: no cover - 状态机穷举
            raise BatchError(
                "STATE_CONFLICT",
                f"记录 {record.record_key} 的状态 {record.status!r} 无法恢复",
                details={"record_key": record.record_key, "status": record.status},
            )

        code = (record.failure or {}).get("code")
        if code in NEW_BATCH_REQUIRED_CODES:
            skipped_new_batch += 1
            continue

        # `failure_stage` 在库里存的是 `stage1`／`stage2` 这种名字（同一个 CHECK 约束），
        # 不是序号；转换统一走 records 模块，避免两处各写一遍映射。
        stage = records_repo.stage_number(record.failure_stage or "stage1")
        campaign = records_repo.latest_campaign(connection, record.record_key, stage)
        used = (
            0
            if campaign is None
            else records_repo.count_campaign_attempts(connection, campaign.campaign_id)
        )
        limit = 0 if campaign is None else campaign.max_attempts

        if campaign is not None and used < limit:
            # 旧 campaign 还有次数：直接继续，**不重开**。
            selected.append(record.record_key)
        elif retry_failed:
            selected.append(record.record_key)
            reopen.append((record.record_key, stage))
        else:
            skipped_budget += 1

    finalize_only = (
        not selected
        and pending_rows == 0
        and run.status in FINALIZE_ONLY_RUN_STATUSES
    )

    return ResumePlan(
        run_id=run_id,
        run_status=run.status,
        retry_failed=retry_failed,
        selected_records=tuple(selected),
        reopen=tuple(reopen),
        skipped_budget_exhausted=skipped_budget,
        skipped_needs_new_batch=skipped_new_batch,
        finalize_only=finalize_only,
    )


def _check_prompt_snapshot(connection: sqlite3.Connection, run_id: str) -> None:
    """恢复**必须**沿用批次快照；快照不完整就拒绝入队（[plan/10 §6]）。

    这一层只查批次级快照是否可用；记录级的 schema 版本差异由
    `services.judging.execute_record` 在执行时逐条拒绝，两边都不猜测。
    """
    snapshot = runs_repo.get_prompt_snapshot(connection, run_id)
    for stage in ("stage1", "stage2"):
        entry = snapshot.get(stage) or {}
        if not entry.get("text") or not entry.get("sha256"):
            raise BatchError(
                "VERSION_INCOMPATIBLE",
                f"批次 {run_id} 的提示词快照缺少 {stage} 正文，无法恢复；请新建批次",
                details={"run_id": run_id, "stage": stage},
            )


@dataclass(frozen=True)
class ResumeResult:
    """一次恢复入队的结果；只表示**已入队**，不表示已经调用模型。"""

    run_id: str
    job_id: str
    selected_records: int
    renewed_campaigns: int
    skipped_budget_exhausted: int
    skipped_needs_new_batch: int
    finalize_only: bool
    dispatch_paused: bool
    reused: bool = False
    http_status: int = 202

    def response_data(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "selected_records": self.selected_records,
            "renewed_campaigns": self.renewed_campaigns,
            "skipped_budget_exhausted": self.skipped_budget_exhausted,
            "skipped_needs_new_batch": self.skipped_needs_new_batch,
            "finalize_only": self.finalize_only,
            "dispatch_paused": self.dispatch_paused,
            "reused": self.reused,
        }


def resume_run(
    database: Database,
    *,
    run_id: str,
    retry_failed: bool = False,
    idempotency_key: str | None = None,
    request_sha256: str | None = None,
) -> ResumeResult:
    """把一次恢复的目标与重开计划入队；**不在点击时重开预算**。

    计划写在 `jobs.payload_json` 里，由 worker 在认领时落实（[plan/10 §6]）：
    这样「点了恢复但 worker 没起来」不会留下已经花掉一轮预算的记录。

    入队、`run` 状态与幂等键**同一事务**提交；`finished_at` 在入队时清空
    （[plan/08 §5]「终止时间在恢复入队时清空」），`created_at` 不变。
    """
    connection = database.connect()
    try:
        if idempotency_key:
            existing = jobs_repo.get_idempotent(
                connection, scope=_resume_scope(run_id), key=idempotency_key
            )
            if existing is not None:
                if existing.request_sha256 != (request_sha256 or ""):
                    raise BatchError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 Idempotency-Key 已用于不同的请求体",
                        details={"key": idempotency_key, "run_id": run_id},
                    )
                data = existing.response_data
                return ResumeResult(
                    run_id=str(data["run_id"]),
                    job_id=str(data["job_id"]),
                    selected_records=int(data["selected_records"]),
                    renewed_campaigns=int(data["renewed_campaigns"]),
                    skipped_budget_exhausted=int(data["skipped_budget_exhausted"]),
                    skipped_needs_new_batch=int(data.get("skipped_needs_new_batch", 0)),
                    finalize_only=bool(data["finalize_only"]),
                    dispatch_paused=bool(data.get("dispatch_paused", False)),
                    reused=True,
                    http_status=existing.http_status,
                )

        try:
            with write_transaction(connection) as conn:
                plan = plan_resume(conn, run_id, retry_failed=retry_failed)
                if not plan.has_work:
                    raise BatchError(
                        "NOTHING_TO_RESUME",
                        f"批次 {run_id} 没有可恢复的记录：{plan.reason_when_empty}",
                        details={
                            "skipped_budget_exhausted": plan.skipped_budget_exhausted,
                            "skipped_needs_new_batch": plan.skipped_needs_new_batch,
                        },
                    )

                job_id = jobs_repo.new_job_id()
                created_at = utc_now()
                state = jobs_repo.get_runtime_state(conn)
                jobs_repo.insert_job(
                    conn,
                    job_id=job_id,
                    run_id=run_id,
                    kind="classify",
                    mode="retry_failed" if retry_failed else "resume",
                    payload={
                        "record_keys": list(plan.selected_records),
                        "retry_failed": retry_failed,
                        "finalize_only": plan.finalize_only,
                        # 重开计划在这里**只记录**，落实在 worker 认领时。
                        "reopen": {
                            key: [stage] for key, stage in plan.reopen
                        },
                    },
                    created_at=created_at,
                )
                runs_repo.reopen_run(conn, run_id=run_id)

                result = ResumeResult(
                    run_id=run_id,
                    job_id=job_id,
                    selected_records=len(plan.selected_records),
                    renewed_campaigns=plan.renewed_campaigns,
                    skipped_budget_exhausted=plan.skipped_budget_exhausted,
                    skipped_needs_new_batch=plan.skipped_needs_new_batch,
                    finalize_only=plan.finalize_only,
                    dispatch_paused=state.model_dispatch_paused,
                )
                if idempotency_key:
                    jobs_repo.put_idempotent(
                        conn,
                        scope=_resume_scope(run_id),
                        key=idempotency_key,
                        request_sha256=request_sha256 or "",
                        http_status=202,
                        response_data=result.response_data(),
                        created_at=created_at,
                        run_id=run_id,
                        job_id=job_id,
                    )
                return result
        except sqlite3.IntegrityError as exc:
            # uq_active_classification 兜底：并发下两次恢复只会成功一次。
            raise BatchError(
                "STATE_CONFLICT",
                "该批次已有排队或运行中的判别任务",
                details={"reason": str(exc), "run_id": run_id},
            ) from exc
    except DatabaseBusyError as exc:
        raise BatchError("DB_BUSY", str(exc), http_status=503) from exc
    finally:
        connection.close()


def _export_action(active_export: jobs_repo.JobRow | None) -> dict[str, Any]:
    """`can_export` 与禁用原因（S05-04）。

    导出与判别**互不阻塞**：判别正在跑时照样可以导出一份当前进度的快照（[plan/08
    §7]，快照在 worker 认领时才捕获）。唯一挡住按钮的是**已经有一份导出在飞**——
    见 `exports.create_manual_export` 的同名检查；两处用同一条规则，避免出现
    「按钮说能导出、点了却 409」。
    """
    if active_export is None:
        return {"can_export": True, "export_disabled_reason": None}
    return {
        "can_export": False,
        "export_disabled_reason": "该批次已有排队或运行中的导出任务，等它完成后可再导出一份",
    }


def allowed_actions(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, Any]:
    """批次详情里的 `allowed_actions`（[plan/08 §5]）。

    与提交路径共用 `plan_resume`：界面上显示的选中数、重开数与提交时的复查
    走同一份逻辑，不会出现「按钮说能恢复、点了却拒绝」。导出同理，见 `_export_action`。
    """
    run = runs_repo.get_run(connection, run_id)
    if run is None:
        raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)

    active = jobs_repo.find_active_classification(connection, run_id)
    active_export = jobs_repo.find_active_job(
        connection, run_id, kind=jobs_repo.EXPORT_KIND
    )
    state = jobs_repo.get_runtime_state(connection)
    counts = records_repo.count_by_status(connection, run_id)

    if active is not None:
        return {
            "can_resume": False,
            "can_retry_failed": False,
            "finalization_required": False,
            "selected_records": 0,
            "renewed_campaigns": 0,
            "skipped_budget_exhausted": 0,
            "skipped_needs_new_batch": 0,
            "disabled_reason": "该批次已有排队或运行中的判别任务",
            "model_dispatch_paused": state.model_dispatch_paused,
            **_export_action(active_export),
        }

    if run.status not in RESUMABLE_RUN_STATUSES:
        return {
            "can_resume": False,
            "can_retry_failed": False,
            "finalization_required": False,
            "selected_records": 0,
            "renewed_campaigns": 0,
            "skipped_budget_exhausted": 0,
            "skipped_needs_new_batch": 0,
            "disabled_reason": "批次已结束且没有可恢复的记录",
            "model_dispatch_paused": state.model_dispatch_paused,
            **_export_action(active_export),
        }

    plain = plan_resume(connection, run_id, retry_failed=False)
    retry = plan_resume(connection, run_id, retry_failed=True)
    return {
        "can_resume": plain.has_work,
        "can_retry_failed": retry.has_work and bool(retry.reopen),
        "finalization_required": plain.finalize_only,
        "selected_records": len(plain.selected_records),
        "renewed_campaigns": plain.renewed_campaigns,
        "skipped_budget_exhausted": plain.skipped_budget_exhausted,
        "skipped_needs_new_batch": plain.skipped_needs_new_batch,
        "retry_failed_selected": len(retry.selected_records),
        "retry_failed_renewed": retry.renewed_campaigns,
        "remaining_rows": counts["pending"] + counts["stage1_done"],
        "disabled_reason": None if plain.has_work else plain.reason_when_empty,
        "model_dispatch_paused": state.model_dispatch_paused,
        **_export_action(active_export),
    }

# --------------------------------------------- 批次列表与详情（S04-07）

#: 合法批次状态；与 `runs` 表的 CHECK 同值（[plan/08 §5]）。
RUN_STATUSES = frozenset(
    {"queued", "running", "completed", "partial_failed", "failed", "interrupted"}
)

#: 列表分页：默认 50，上限 100（[plan/08 §5]）。
RUN_PAGE_SIZE_DEFAULT = 50
RUN_PAGE_SIZE_MAX = 100

#: 记录列表分页：默认 50、上限 200（S06 阶段文档 §0.3 第 2 项，用户一并确认）。
#: 上限比批次列表高：一个批次动辄上千条记录，逐条核对时翻页次数直接决定可用性。
RECORD_PAGE_SIZE_DEFAULT = 50
RECORD_PAGE_SIZE_MAX = 200

#: `records.status` 合法值；与建表 CHECK 同值（[plan/09 §3]）。
RECORD_STATUSES = frozenset(
    {"pending", "input_invalid", "stage1_done", "completed", "failed"}
)

#: 合法三分类；与 `schemas/judgement.py` 的唯一定义同源，不在这里另抄一份。
RECORD_LABELS = LABELS

#: 记录列表里 `q` 的摘要长度。取 200 是「一眼认出是哪条」的长度，不是渲染限制
#: （列宽由界面决定）；摘要会把连续空白压成单个空格，所以它不保证是 `q` 的逐字切片。
Q_PREVIEW_LENGTH = 200

#: 详情里最多列出的失败记录条数与最近任务条数；超过部分只报数量，不悄悄截断。
FAILURE_SUMMARY_LIMIT = 50
RECENT_JOB_LIMIT = 10

#: `model_config` 只取模型与已验证的非敏感参数：`config_snapshot` 里的本机绝对
#: 路径与配置文件位置不属于接口合同，读接口不把它们发出去（[plan/08 §5]）。
_MODEL_CONFIG_KEYS = ("stage1", "stage2", "execution")


def counts_of(
    run: runs_repo.RunRow, by_status: dict[str, int], review_required: int
) -> dict[str, int]:
    """批次计数；口径固定为 [plan/08 §5]。

    ``processed = classified + failed + input_invalid``、``remaining = total -
    processed``；``review_required`` 是 ``classified`` 的子集，不加进 ``processed``。
    计数与状态在同一读事务里取（调用方负责），因此不会出现「failed=1 而失败列表为空」。

    **导出也用它**（S05）：概况表的计数与界面显示的数字必须是同一个公式，否则
    「界面说剩 3 条、概况表说剩 2 条」这种偏差只能靠人去发现。
    """
    classified = by_status["completed"]
    failed = by_status["failed"]
    input_invalid = by_status["input_invalid"]
    processed = classified + failed + input_invalid
    return {
        "total": run.total_count,
        "valid": run.valid_count,
        "input_invalid": input_invalid,
        "classified": classified,
        "failed": failed,
        "remaining": run.total_count - processed,
        "processed": processed,
        "review_required": review_required,
    }


def _progress_percent(counts: dict[str, int]) -> float:
    """`processed / total × 100`，保留一位小数；total 为 0 时按 0 处理。"""
    total = counts["total"]
    if total <= 0:  # pragma: no cover - valid_count >= 1 保证 total >= 1
        return 0.0
    return round(counts["processed"] / total * 100, 1)


def _job_summary(job: jobs_repo.JobRow) -> dict[str, Any]:
    """任务摘要；**不含 payload**（里面是整批记录键，界面用不到）。

    `run_id` 在 [plan/08 §7] 的任务字段里，因此**单独查任务**（S06-02 的
    `job_detail`）时也能看出它属于哪个批次；批次详情里这个字段是冗余的，
    但两处共用同一个形状，界面与测试不必按入口分辨字段。
    """
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "kind": job.kind,
        "mode": job.mode,
        "status": job.status,
        "current_record_key": job.current_record_key,
        "current_stage": job.current_stage,
        "worker_id": job.worker_id,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "last_activity_at": job.last_activity_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "result": job.result,
    }


def _execution_control(state: jobs_repo.RuntimeState) -> dict[str, Any]:
    """模型消费闸门与**最近一次** worker 归属（[plan/08 §5]）。

    只报事实：worker 标识、上线时间、模式。是否「进程已经退出」在本机无法由库里的
    时间戳判定（worker 可能正在处理一条长请求），所以这里不下结论，界面也不得据
    时间戳断言进程死亡（S04-07 定稿要点）。
    """
    return {
        "model_dispatch_paused": state.model_dispatch_paused,
        "pause_reason": state.pause_reason,
        "last_worker": (
            None
            if state.worker_id is None
            else {
                "worker_id": state.worker_id,
                "started_at": state.worker_started_at,
                "mode": state.worker_mode,
            }
        ),
        "runtime_updated_at": state.updated_at,
    }


def _model_config(config_snapshot: dict[str, Any]) -> dict[str, Any]:
    """批次创建时冻结的模型与执行参数；**凭据只以「是否存在」的形式出现**。"""
    return {key: config_snapshot.get(key) for key in _MODEL_CONFIG_KEYS}


def list_batch_runs(
    database: Database,
    *,
    page: int = 1,
    page_size: int = RUN_PAGE_SIZE_DEFAULT,
    status: str | None = None,
) -> dict[str, Any]:
    """`GET /api/runs`：分页批次列表（[plan/08 §5]）。

    分页响应统一 ``items/page/page_size/total``；`status` 非法即 422（不静默忽略
    筛选条件——那会让用户以为没有数据）。
    """
    if status is not None and status not in RUN_STATUSES:
        raise BatchError(
            "PARAM_VALIDATION",
            f"未知批次状态 {status!r}；只接受 {'、'.join(sorted(RUN_STATUSES))}",
            http_status=422,
            details={"allowed": sorted(RUN_STATUSES)},
        )
    if page < 1 or page_size < 1:
        raise BatchError("PARAM_VALIDATION", "page 与 page_size 必须是正整数", http_status=422)
    if page_size > RUN_PAGE_SIZE_MAX:
        raise BatchError(
            "PARAM_VALIDATION",
            f"page_size 上限为 {RUN_PAGE_SIZE_MAX}，收到 {page_size}",
            http_status=422,
        )

    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            rows, total = runs_repo.list_runs(
                snapshot,
                limit=page_size,
                offset=(page - 1) * page_size,
                status=status,
            )
            items = []
            for run in rows:
                by_status = records_repo.count_by_status(snapshot, run.run_id)
                counts = counts_of(
                    run,
                    by_status,
                    records_repo.count_review_required(snapshot, run.run_id),
                )
                items.append(
                    {
                        "run_id": run.run_id,
                        "original_filename": run.source_filename,
                        "sheet_name": run.sheet_name,
                        "status": run.status,
                        "revision": run.revision,
                        "counts": counts,
                        "progress_percent": _progress_percent(counts),
                        "created_at": run.created_at,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                    }
                )
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    finally:
        connection.close()


def run_detail(
    database: Database,
    run_id: str,
    *,
    failure_limit: int = FAILURE_SUMMARY_LIMIT,
) -> dict[str, Any]:
    """`GET /api/runs/{run_id}`：批次详情（[plan/08 §5] + S04-07 界面清单）。

    比 plan/08 §5 多三项，都是界面清单要求的可核对值，**不含证据正文**：

    - ``failure_summary``：失败记录的编号 + 失败阶段 + 错误码；
    - ``call_statistics``：阶段一/阶段二各自的尝试数与成功数（恢复前后核对阶段一
      没有被重复调用）；
    - ``recent_jobs``：该批次最近的任务（``active_job`` 在批次结束后为 null，
      界面仍需要看到刚跑完那个任务落到了什么状态）。

    ``latest_export`` 是该批次最近一次导出（[plan/08 §7]）：终态那次自动导出与
    用户在界面上点的手动导出都算，一次都没排过时为 ``null``。**捕获前**（导出任务
    还在排队或运行）里面只有 `job_status`，`captured_*` 为 null——界面据此显示
    「尚未捕获快照」，不把 `scheduled_revision` 冒充成已捕获的 revision。
    """
    # 局部导入：`services.exports` 在模块层要用本模块的 `counts_of`（快照与界面共用
    # 同一个计数公式），模块层互相导入会成环。这里只在真正读详情时才需要它。
    from .exports import latest_export_view

    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            run = runs_repo.get_run(snapshot, run_id)
            if run is None:
                raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)

            by_status = records_repo.count_by_status(snapshot, run_id)
            counts = counts_of(
                run, by_status, records_repo.count_review_required(snapshot, run_id)
            )
            failure_rows = records_repo.list_record_failures(
                snapshot, run_id, limit=failure_limit
            )
            failure_total = records_repo.count_record_failures(snapshot, run_id)
            active = jobs_repo.find_active_classification(snapshot, run_id)
            recent = [
                _job_summary(job)
                for job in reversed(
                    jobs_repo.list_jobs_for_run(snapshot, run_id)[-RECENT_JOB_LIMIT:]
                )
            ]
            return {
                "run_id": run.run_id,
                "original_filename": run.source_filename,
                "sheet_name": run.sheet_name,
                "status": run.status,
                "revision": run.revision,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "counts": counts,
                "progress_percent": _progress_percent(counts),
                "active_job": None if active is None else _job_summary(active),
                "recent_jobs": recent,
                "execution_control": _execution_control(
                    jobs_repo.get_runtime_state(snapshot)
                ),
                "model_config": _model_config(run.config_snapshot),
                "versions": run.versions,
                "last_error": run.last_error,
                "allowed_actions": allowed_actions(snapshot, run_id),
                "failure_summary": {
                    "count": failure_total,
                    "items": [
                        {
                            "record_key": row.record_key,
                            "record_id": row.record_id,
                            "source_row": row.source_row,
                            "order_index": row.order_index,
                            "failure_stage": row.failure_stage,
                            "code": (row.failure or {}).get("code"),
                            "message": (row.failure or {}).get("message"),
                            "retryable": (row.failure or {}).get("retryable"),
                            "attempt_count": (row.failure or {}).get("attempt_count"),
                        }
                        for row in failure_rows
                    ],
                    "truncated": failure_total > len(failure_rows),
                },
                "call_statistics": records_repo.attempt_statistics(snapshot, run_id),
                "latest_export": latest_export_view(snapshot, run_id),
            }
    finally:
        connection.close()


def job_detail(database: Database, job_id: str) -> dict[str, Any]:
    """`GET /api/jobs/{job_id}`：**按库**返回任务（S06-02，[plan/08 §7]）。

    S03-07 的实现只查 API 进程的内存注册表，落库的批次任务查不到；``/api/judge``
    删除后所有任务都在 `jobs` 表里，这里就是唯一读法。字段与批次详情的
    ``active_job``／``recent_jobs`` **共用 `_job_summary`**，两处不会各自漂移。

    **``mode`` 不是模拟/真实**：判别任务是 `initial`／`resume`／`retry_failed`，
    导出任务是 `automatic`／`manual`。要判断模拟/真实请看批次详情的
    `call_statistics.*.simulated` 与 `execution_control.last_worker.mode`。
    """
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            job = jobs_repo.get_job(snapshot, job_id)
            if job is None:
                raise BatchError("NOT_FOUND", f"未知 job_id：{job_id}", http_status=404)
            return _job_summary(job)
    finally:
        connection.close()


# ------------------------------------------------ 记录查询（S06-01，plan/08 §6）


def _q_preview(text: str | None) -> str | None:
    """列表里的 `q` 摘要；`None` 原样返回（输入失败行没有 q）。"""
    if text is None:
        return None
    flat = " ".join(text.split())
    if len(flat) <= Q_PREVIEW_LENGTH:
        return flat
    return flat[:Q_PREVIEW_LENGTH] + "…"


def _stage2_reason(stage2: records_repo.StageResultRow | None) -> str | None:
    """阶段二的理由原文；没有阶段二结果（未跑完／失败）时为 `None`。"""
    if stage2 is None:
        return None
    payload = json.loads(stage2.result_json)
    value = payload.get("reason")
    return None if value is None else str(value)


def _attempt_outcome(status: str, code: Any) -> str:
    """库里的尝试终态 → 界面口径的结果名。

    与 `agent/pipeline.py` 的 `AttemptOutcome` 对齐，另加 `unknown_after_interrupt`：
    那是「调用已发出但进程在拿到结果前被杀」，不是模型或校验的错误，硬塞进
    `model_error` 会让人以为服务返回了失败。
    """
    if status == records_repo.ATTEMPT_SUCCEEDED:
        return "ok"
    if status == records_repo.ATTEMPT_UNKNOWN:
        return "unknown_after_interrupt"
    return "validation_error" if code == CODE_OUTPUT_INVALID else "model_error"


def _raw_output_of(attempt: records_repo.AttemptRow) -> tuple[str | None, bool]:
    """被**校验拒绝**的那几次尝试的模型正文；其余返回 ``None``。

    库里每条尝试都存了 `final_content`（成功的那次存的是模型返回的合格正文），
    但接口只暴露「被拒原文」这一种——成功尝试的正文不是失败证据，且把每次成功调用
    的正文都发出去等于把整批模型输出复制一份到浏览器。判据与 S03 返工一致：
    终态是 `failed` 且错误码是 `OUTPUT_INVALID`。

    是否截断靠标记判断（库里没有这一列，见 `RAW_OUTPUT_ELLIPSIS` 的说明）。
    """
    if attempt.status != records_repo.ATTEMPT_FAILED:
        return None, False
    if (attempt.error or {}).get("code") != CODE_OUTPUT_INVALID:
        return None, False
    text = attempt.final_content
    if not text:
        return None, False
    return text, RAW_OUTPUT_ELLIPSIS in text


def _attempt_entries(
    attempts_by_stage: dict[int, tuple[records_repo.AttemptRow, ...]],
) -> list[dict[str, Any]]:
    """调用尝试摘要；**形状沿用** `agent/pipeline.py::result_to_payload()`。

    界面在 S03 返工里就是按这个形状渲染「第几次尝试 / 耗时 / 被拒原文」的，批次
    路径沿用它，同一份界面组件不必为两条路径写两套读取逻辑。
    """
    entries: list[dict[str, Any]] = []
    for stage in sorted(attempts_by_stage):
        for attempt in attempts_by_stage[stage]:
            error = attempt.error or {}
            code = error.get("code")
            raw_output, truncated = _raw_output_of(attempt)
            entries.append(
                {
                    "stage": records_repo.stage_name(stage),
                    "attempt": attempt.attempt_no,
                    "outcome": _attempt_outcome(attempt.status, code),
                    "model": attempt.returned_model_id,
                    "latency_ms": attempt.duration_ms,
                    "simulated": attempt.simulated,
                    "usage": attempt.usage,
                    "error_code": code,
                    "error_message": error.get("message"),
                    "raw_output": raw_output,
                    "raw_output_truncated": truncated,
                }
            )
    return entries


def _record_summary(
    row: records_repo.RecordRow,
    stage2: records_repo.StageResultRow | None,
) -> dict[str, Any]:
    """列表摘要；字段与 [plan/08 §6] 逐条对应，**不含 `a` 与任何 ref**。

    ``failure`` 只放技术失败（`failed` 行的 `failure_json`）。输入失败行不在此列：
    它的 `status` 已经是 `input_invalid`，把两种成因混进同一个字段会让「失败 3 条」
    这种计数无法解释。原文与输入失败原因在记录详情里各有一个字段。
    """
    return {
        "record_key": row.record_key,
        "record_id": row.record_id,
        "source_row": row.source_row,
        "order_index": row.order_index,
        "q_preview": _q_preview(row.q),
        "status": row.status,
        "label": row.final_label,
        "reason": _stage2_reason(stage2),
        "review_required": (
            None if row.review_required is None else bool(row.review_required)
        ),
        "failure": row.failure,
    }


def list_run_records(
    database: Database,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = RECORD_PAGE_SIZE_DEFAULT,
    label: str | None = None,
    review_required: bool | None = None,
    status: str | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    """`GET /api/runs/{run_id}/records`：分页 + 筛选的记录列表（[plan/08 §6]）。

    除分页四项外**多返回 `revision`**：界面靠它判断「翻页时批次是否又变了」，
    避免把不同轮次的统计拼成一份最终报告（[plan/08 §6] 结尾）。

    非法枚举与分页值一律 422，不静默忽略筛选条件——把 `label=xxx` 当成「没有筛选」
    会让用户以为筛出来的就是全部。
    """
    if label is not None and label not in RECORD_LABELS:
        raise BatchError(
            "PARAM_VALIDATION",
            f"未知标签 {label!r}；只接受 {'、'.join(RECORD_LABELS)}",
            http_status=422,
            details={"allowed": list(RECORD_LABELS)},
        )
    if status is not None and status not in RECORD_STATUSES:
        raise BatchError(
            "PARAM_VALIDATION",
            f"未知记录状态 {status!r}；只接受 {'、'.join(sorted(RECORD_STATUSES))}",
            http_status=422,
            details={"allowed": sorted(RECORD_STATUSES)},
        )
    if page < 1 or page_size < 1:
        raise BatchError("PARAM_VALIDATION", "page 与 page_size 必须是正整数", http_status=422)
    if page_size > RECORD_PAGE_SIZE_MAX:
        raise BatchError(
            "PARAM_VALIDATION",
            f"page_size 上限为 {RECORD_PAGE_SIZE_MAX}，收到 {page_size}",
            http_status=422,
        )

    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            run = runs_repo.get_run(snapshot, run_id)
            if run is None:
                raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)

            rows, total = records_repo.list_records_page(
                snapshot,
                run_id,
                limit=page_size,
                offset=(page - 1) * page_size,
                label=label,
                review_required=review_required,
                status=status,
                record_id=record_id,
            )
            stage2_results = records_repo.list_stage_results(
                snapshot, [row.record_key for row in rows], 2
            )
            items = [
                _record_summary(row, stage2_results.get(row.record_key)) for row in rows
            ]
            revision = run.revision
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "revision": revision,
        }
    finally:
        connection.close()


def record_detail(database: Database, run_id: str, record_key: str) -> dict[str, Any]:
    """`GET /api/runs/{run_id}/records/{record_key}`：单条记录的完整证据（[plan/08 §6]）。

    `record_key` 是**内部 UUID**（`records.record_key`），不是预检返回的可读键：
    业务编号可能缺失或含不适合放进 URL 的字符（[plan/08 §1]）。

    **不发**模型推理链、请求头与 SDK 调试堆栈。`stage1`／`stage2` 直接回
    `stage_results.result_json` 解析后的原文——阶段二的 `stage1_corrections` 必须
    原样保留，重新按模型序列化会把「更正」压平（导出层当初也是这么处理的）。

    `input_error` 是输入失败行的原因（与预检报告 `row_errors[].reason` 同源，本来
    就经 `/api/uploads/{id}/validate` 公开）。它不进 `failure`：`failure` 是技术失败，
    两者在导出与计数里一直分开（见 `_failure_rows` 的注释）。
    """
    connection = database.connect()
    try:
        with read_transaction(connection) as snapshot:
            run = runs_repo.get_run(snapshot, run_id)
            if run is None:
                raise BatchError("NOT_FOUND", f"未知 run_id：{run_id}", http_status=404)

            row = records_repo.get_record(snapshot, record_key)
            if row is None or row.run_id != run_id:
                # 记录存在但不属于该批次：对调用方而言就是「这个批次里没有这条」，
                # 不区分「不存在」与「不在本批次」，避免把别的批次的存在性漏出去。
                raise BatchError(
                    "NOT_FOUND",
                    f"批次 {run_id} 中不存在记录 {record_key}",
                    http_status=404,
                )

            stage1 = records_repo.get_stage_result(snapshot, record_key, 1)
            stage2 = records_repo.get_stage_result(snapshot, record_key, 2)
            attempts = records_repo.list_attempts_by_stage(snapshot, record_key)
            # 输入用**建批次时留下的快照**（`raw_input_json`，13 个输入列原文），
            # 不按当前模型重新拼：输入失败行的 q/a 本来就是空，重拼只会得到同一份。
            input_snapshot = dict(row.raw_input)
            revision = run.revision
        return {
            "run_id": run_id,
            "record_key": row.record_key,
            "record_id": row.record_id,
            "source_row": row.source_row,
            "order_index": row.order_index,
            "status": row.status,
            "revision": revision,
            "input": input_snapshot,
            "label": row.final_label,
            "review_required": (
                None if row.review_required is None else bool(row.review_required)
            ),
            "failure": row.failure,
            "input_error": row.input_error,
            "stage1": None if stage1 is None else json.loads(stage1.result_json),
            "stage2": None if stage2 is None else json.loads(stage2.result_json),
            "attempt_summary": _attempt_entries(attempts),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    finally:
        connection.close()


__all__ = [
    "FAILURE_SUMMARY_LIMIT",
    "FINALIZE_ONLY_RUN_STATUSES",
    "NEW_BATCH_REQUIRED_CODES",
    "Q_PREVIEW_LENGTH",
    "RECENT_JOB_LIMIT",
    "RECORD_LABELS",
    "RECORD_PAGE_SIZE_DEFAULT",
    "RECORD_PAGE_SIZE_MAX",
    "RECORD_STATUSES",
    "RESUMABLE_RUN_STATUSES",
    "RUN_CREATE_SCOPE",
    "RUN_PAGE_SIZE_DEFAULT",
    "RUN_PAGE_SIZE_MAX",
    "RUN_STATUSES",
    "BatchError",
    "ResumePlan",
    "ResumeResult",
    "RunCreation",
    "allowed_actions",
    "counts_of",
    "create_run",
    "job_detail",
    "list_batch_runs",
    "list_run_records",
    "load_parsed_input",
    "plan_resume",
    "record_detail",
    "resume_run",
    "run_detail",
]
