"""S06-02：诊断产物写入迁到 **worker 的失败路径**（S03 返工 R-5 的成果）。

S03 时这套写入挂在临时接口（`POST /api/judge`）的内存注册表上，落库批次判失败
**不落任何诊断产物**。本条改动搬到 worker，判定口径不变：干净成功不写，
其余都写（含「某次被拒、重试后成功」）。

要点：

- 文件名 ``judge-<job_id>-<record_key>.json``——一个任务多条失败记录时**不互相覆盖**；
- 写盘失败只记日志，**绝不改变任务状态**（记录照常失败、任务照常收尾）；
- 内容与记录详情同源（`result_to_payload()` 的形状），只含**最终 content**，
  不含推理链。

全部使用合成工作簿、本地 SQLite 与模拟客户端，**零真实模型调用**。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

import pytest

from aidhu_om_agent.agent.diagnostics import needs_diagnostic, write_record_diagnostic
from aidhu_om_agent.llm.client import STAGE1, STAGE2, ModelResponse
from aidhu_om_agent.schemas.qa import REF_FIELDS, QARecord
from aidhu_om_agent.storage import utc_now
from aidhu_om_agent.worker import MODE_MOCK
from fixtures.excel_samples import qa_row, write_workbook
from test_batches import make_config
from test_worker import Probe, correct_factory, make_worker, prepare_run

DIAGNOSTIC_DIR = "judge-diagnostics"


class RejectingClient:
    """每次调用都返回过不了校验的文本：让**每条**记录都技术失败。"""

    def __init__(self, record: QARecord) -> None:
        self.record = record
        self.calls: list[str] = []

    def call(
        self, messages: Sequence[Any], stage: str
    ) -> ModelResponse:
        assert stage in (STAGE1, STAGE2)
        self.calls.append(stage)
        return ModelResponse(
            content="这不是 JSON，校验一定拒绝。",
            model="rejecting-client",
            usage=None,
            latency_ms=3,
            finish_reason="stop",
            simulated=True,
        )


def rejecting_factory(record) -> RejectingClient:  # noqa: ANN001 - 测试夹具
    return RejectingClient(record.to_qa_record())


def failing_workbook(path: Path, count: int = 1) -> Path:
    return write_workbook(
        path,
        [qa_row(str(index), f"问题{index}", "回答", ["资料一"]) for index in range(1, count + 1)],
    )


def diagnostic_files(config) -> list[Path]:
    directory = Path(config.paths.runtime) / DIAGNOSTIC_DIR
    return sorted(directory.glob("judge-*.json")) if directory.is_dir() else []


def run_batch(config, *, client_factory) -> None:
    worker = make_worker(config, mode=MODE_MOCK, client_factory=client_factory)
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()


def sample_record(**overrides: Any) -> QARecord:
    """一条最小的有效记录（refs 必须给全 10 项）。"""
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs["ref1"] = "资料一"
    payload: dict[str, Any] = {
        "record_id": "1",
        "source_row": 2,
        "order_index": 0,
        "q": "问题",
        "a": "回答",
        "refs": refs,
    }
    payload.update(overrides)
    return QARecord(**payload)


# ------------------------------------------------------------------ 失败即写


def test_worker_writes_one_diagnostic_per_failed_record(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = failing_workbook(tmp_path / "src" / "two.xlsx", count=2)
    database, created = prepare_run(config, workbook, tmp_path)

    run_batch(config, client_factory=rejecting_factory)

    files = diagnostic_files(config)
    assert len(files) == 2  # 两条失败记录各写一份，不互相覆盖
    keys = {file.name for file in files}
    assert all(name.startswith(f"judge-{created.job_id}-") for name in keys)

    payloads = [json.loads(file.read_text(encoding="utf-8")) for file in files]
    assert {item["record_key"] for item in payloads} == {
        name[len(f"judge-{created.job_id}-") : -len(".json")] for name in keys
    }
    assert {item["job_id"] for item in payloads} == {created.job_id}
    assert {item["run_id"] for item in payloads} == {created.run_id}
    assert {item["mode"] for item in payloads} == {MODE_MOCK}  # worker 实际模式
    assert {item["record_status"] for item in payloads} == {"failed"}
    assert {item["record_id"] for item in payloads} == {"1", "2"}
    # 任务状态要到 `_finalize` 才定，诊断里**不写**，免得出现「已完成的失败记录」。
    assert all("job_status" not in item for item in payloads)
    assert all(item["failure"] is not None for item in payloads)


def test_diagnostic_records_final_content_and_quote_facts(tmp_path: Path) -> None:
    """诊断里的原文诊断是**字符级事实**：够人判读被拒的原因，不写推测。"""
    config = make_config(tmp_path)
    workbook = failing_workbook(tmp_path / "src" / "one.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)

    run_batch(config, client_factory=rejecting_factory)

    (file,) = diagnostic_files(config)
    document = json.loads(file.read_text(encoding="utf-8"))

    assert document["attempts"]
    attempt = document["attempts"][0]
    # 与记录详情同一形状：只带最终 content，不带推理链或请求头。
    assert {
        "stage",
        "attempt",
        "outcome",
        "latency_ms",
        "error_code",
        "error_message",
        "raw_output",
        "raw_output_truncated",
        "quote_diagnosis",
    } == set(attempt)
    assert attempt["outcome"] != "ok"
    assert attempt["raw_output"] == "这不是 JSON，校验一定拒绝。"
    assert attempt["quote_diagnosis"]["parseable"] is False  # 取不出 JSON 就如实说
    assert set(document["prompt_versions"]) == {"stage1", "stage2"}
    assert set(document["schema_versions"]) == {"stage1", "stage2"}


def test_clean_success_writes_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = failing_workbook(tmp_path / "src" / "ok.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)

    run_batch(config, client_factory=correct_factory())

    probe = Probe(database)
    assert probe.scalar(
        "SELECT status FROM records WHERE run_id = ?", (created.run_id,)
    ) == "completed"
    assert diagnostic_files(config) == []


# ------------------------------------------------------------------ 写盘失败


def test_write_failure_changes_no_state(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """诊断写不出去只记日志：记录照样失败、任务照样收尾，不因观测手段失真。"""
    config = make_config(tmp_path)
    workbook = failing_workbook(tmp_path / "src" / "blocked.xlsx")
    database, created = prepare_run(config, workbook, tmp_path)
    # 用**普通文件**占住目录名：`mkdir` 必然失败，走真实的 try/except。
    blocked = Path(config.paths.runtime) / DIAGNOSTIC_DIR
    blocked.write_text("占位文件", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="aidhu_om_agent.agent.diagnostics"):
        run_batch(config, client_factory=rejecting_factory)

    probe = Probe(database)
    # 按 job_id 定位那一行：收尾时自动排出的导出任务也是 `jobs` 里的一行。
    assert (
        probe.scalar("SELECT status FROM jobs WHERE job_id = ?", (created.job_id,))
        == "partial_failed"
    )
    assert (
        probe.scalar("SELECT status FROM records WHERE run_id = ?", (created.run_id,))
        == "failed"
    )
    warnings = [
        item for item in caplog.records if item.name == "aidhu_om_agent.agent.diagnostics"
    ]
    assert any("诊断产物写入失败" in item.getMessage() for item in warnings)
    assert blocked.read_text(encoding="utf-8") == "占位文件"  # 没被覆盖


def test_needs_diagnostic_rule_is_unchanged() -> None:
    """口径与 S03 相同：干净成功不写，其余（含重试后成功）都写。"""
    clean = {"status": "completed", "attempts": [{"outcome": "ok"}, {"outcome": "ok"}]}
    retried = {"status": "completed", "attempts": [{"outcome": "rejected"}, {"outcome": "ok"}]}
    failed = {"status": "failed", "attempts": [{"outcome": "rejected"}]}

    assert needs_diagnostic(clean) is False
    assert needs_diagnostic(retried) is True
    assert needs_diagnostic(failed) is True


def test_missing_runtime_root_is_created(tmp_path: Path) -> None:
    """目录不存在时按需建；返回写出的路径，成功路径不抛错。"""
    runtime = tmp_path / "runtime" / "deep"

    written = write_record_diagnostic(
        runtime_root=runtime,
        job_id="job-1",
        run_id="run-1",
        record_key="record-1",
        record=sample_record(),
        payload={
            "record_id": "1",
            "source_row": 2,
            "status": "failed",
            "failure": {"code": "OUTPUT_INVALID", "stage": "stage2"},
            "attempts": [
                {
                    "stage": "stage2",
                    "attempt": 1,
                    "outcome": "rejected",
                    "model": "m",
                    "latency_ms": 1,
                    "simulated": True,
                    "usage": None,
                    "error_code": "OUTPUT_INVALID",
                    "error_message": "输出不是合法 JSON",
                    "raw_output": "被拒原文",
                    "raw_output_truncated": False,
                }
            ],
        },
        mode=MODE_MOCK,
        written_at=utc_now(),
    )

    assert written is not None
    assert written == runtime / DIAGNOSTIC_DIR / "judge-job-1-record-1.json"
    assert written.is_file()
