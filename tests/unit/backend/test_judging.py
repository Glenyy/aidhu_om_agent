"""2026-10-06 返工：判一条任务的耗时、尝试次数、被拒输出与诊断产物。

全部走 ``mock`` 客户端，**不产生任何真实调用**。强制失败用例用模拟模式里刻意
被拒的输出复现真实的 ``OUTPUT_INVALID`` 路径，使失败界面零成本可验。

注意层级：``job.payload()`` 是**任务**视图（时间、状态、尝试），记录级结果
（``label``/``stage1``/``failure``/``attempts``）在它的 ``result`` 里。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from aidhu_om_agent.agent.mock_samples import (
    SCENARIO_FORCED_FAILURE,
    MockClient,
    scenario_for,
)
from aidhu_om_agent.agent.pipeline import CODE_OUTPUT_INVALID
from aidhu_om_agent.config import (
    AppConfig,
    ExecutionConfig,
    LimitsConfig,
    ModelConfig,
    PathsConfig,
)
from aidhu_om_agent.llm.client import STAGE1, STAGE2
from aidhu_om_agent.schemas.qa import REF_FIELDS, QARecord
from aidhu_om_agent.services.judging import (
    COMPLETED,
    FAILED,
    PARTIAL_FAILED,
    JudgeJobRegistry,
    _ReportingClient,
)

REF1 = "校园网密码可在自助服务终端重置，需刷校园卡并输入原密码。"
TERMINAL = {COMPLETED, PARTIAL_FAILED, FAILED}


def make_config(tmp_path: Path) -> AppConfig:
    model = ModelConfig(base_url="", model="test-model", timeout_seconds=1.0, api_key=None)
    return AppConfig(
        stage1=model,
        stage2=model,
        execution=ExecutionConfig(
            concurrency=1, max_attempts_per_stage_campaign=3, retry_backoff_seconds=(0, 0)
        ),
        limits=LimitsConfig(max_upload_bytes=4 * 1024 * 1024, max_records=1000),
        paths=PathsConfig(
            database=tmp_path / "runtime" / "db.sqlite3",
            uploads=tmp_path / "runtime" / "uploads",
            runtime=tmp_path / "runtime",
            outputs=tmp_path / "outputs",
            logs=tmp_path / "logs",
            frontend_dist=tmp_path / "dist",
        ),
        source_config=tmp_path / "config.toml",
        source_env=None,
    )


def make_record(record_id: str = "FF-1") -> QARecord:
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs["ref1"] = REF1
    return QARecord(
        record_id=record_id,
        source_row=2,
        order_index=0,
        q="校园网密码怎么重置？",
        a="在自助服务里重置。",
        refs=refs,
    )


def wait_for_job(registry: JudgeJobRegistry, job_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _ in range(500):
        job = registry.get(job_id)
        assert job is not None
        payload = job.payload()
        if payload["status"] in TERMINAL:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"任务未在预期内结束：{payload.get('status')}")


def run_job(
    tmp_path: Path, record: QARecord | None = None
) -> dict[str, Any]:
    """跑完一条模拟任务，返回终态的**任务** payload。"""
    registry = JudgeJobRegistry(make_config(tmp_path))
    record = record or make_record()
    job = registry.submit(
        record=record, validation_id="v1", record_key=record.record_id or "row:2", mode="mock"
    )
    return wait_for_job(registry, job.job_id)


# ------------------------------------------------------------------ 时间与尝试


def test_terminal_payload_carries_timestamps_and_elapsed(tmp_path: Path) -> None:
    payload = run_job(tmp_path)

    assert payload["created_at"]
    assert payload["started_at"]
    assert payload["finished_at"]
    assert payload["created_at"] <= payload["started_at"] <= payload["finished_at"]
    assert isinstance(payload["elapsed_ms"], int) and payload["elapsed_ms"] >= 0
    # 终态不再显示"正在做第几次尝试"，避免界面把已结束的任务显示成进行中。
    assert payload["current_stage"] is None
    assert payload["current_attempt"] is None


def test_reporting_client_numbers_attempts_per_stage() -> None:
    seen: list[tuple[str, int]] = []
    inner = MockClient(make_record(record_id="1"))
    client = _ReportingClient(inner, lambda stage, attempt: seen.append((stage, attempt)))

    client.call([], STAGE1)
    client.call([], STAGE2)
    client.call([], STAGE1)

    assert seen == [(STAGE1, 1), (STAGE2, 1), (STAGE1, 2)]


def test_failure_also_records_finished_at(tmp_path: Path) -> None:
    payload = run_job(tmp_path)

    assert payload["status"] == PARTIAL_FAILED
    assert payload["finished_at"]
    assert payload["elapsed_ms"] >= 0


# -------------------------------------------------------- 强制失败与技术失败面


def test_forced_failure_record_maps_to_the_forced_scenario() -> None:
    assert scenario_for(make_record()) == SCENARIO_FORCED_FAILURE


def test_forced_failure_produces_output_invalid_without_a_label(tmp_path: Path) -> None:
    payload = run_job(tmp_path)
    result = payload["result"]

    assert payload["status"] == PARTIAL_FAILED
    assert result["label"] is None
    # 失败记录不返回阶段详情（S03 既有行为：不给标签、不半填结果），
    # 但 attempts 里能看到阶段一确实通过了。
    assert result["stage1"] is None and result["stage2"] is None
    assert [(item["stage"], item["outcome"]) for item in result["attempts"]] == [
        (STAGE1, "ok"),
        (STAGE2, "validation_error"),
        (STAGE2, "validation_error"),
        (STAGE2, "validation_error"),
    ]
    assert result["failure"]["code"] == CODE_OUTPUT_INVALID
    assert result["failure"]["stage"] == STAGE2
    assert result["failure"]["attempt_count"] == 3
    assert result["failure"]["retryable"] is False

    rejected = [item for item in result["attempts"] if item["outcome"] == "validation_error"]
    assert len(rejected) == 3
    assert all(item["raw_output"] for item in rejected)
    assert all("真实子串" in (item["error_message"] or "") for item in rejected)


# ------------------------------------------------------------------ 诊断产物


def test_failure_writes_a_diagnostic_artifact(tmp_path: Path) -> None:
    payload = run_job(tmp_path)
    path = tmp_path / "runtime" / "judge-diagnostics" / f"judge-{payload['job_id']}.json"
    assert path.exists()

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["job_id"] == payload["job_id"]
    assert document["record_key"] == "FF-1"
    assert document["mode"] == "mock"
    assert document["job_status"] == PARTIAL_FAILED
    assert document["record_status"] == "failed"
    assert document["failure"]["code"] == CODE_OUTPUT_INVALID
    assert document["elapsed_ms"] == payload["elapsed_ms"]
    assert document["models"] == ["mock-deterministic"]
    assert document["prompt_versions"] and document["schema_versions"]

    rejected = [item for item in document["attempts"] if item["outcome"] == "validation_error"]
    assert len(rejected) == 3
    diagnosis = rejected[-1]["quote_diagnosis"]
    assert diagnosis["parseable"] is True
    assert diagnosis["quotes"], "被拒输出里的证据摘录必须被取出并诊断"
    quote = diagnosis["quotes"][0]
    assert quote["is_substring"] is False
    assert quote["ref_available"] is True
    assert quote["first_divergence_index"] is not None
    assert quote["similarity_ratio"] is not None


def test_clean_success_writes_no_diagnostic_artifact(tmp_path: Path) -> None:
    payload = run_job(tmp_path, record=make_record(record_id="1"))

    assert payload["status"] == COMPLETED
    assert not (tmp_path / "runtime" / "judge-diagnostics").exists()


def test_diagnostic_write_failure_does_not_change_job_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观测手段出问题绝不能反过来改变任务状态。"""

    def explode(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("模拟诊断组件故障")

    monkeypatch.setattr("aidhu_om_agent.services.judging.diagnose_raw_output", explode)
    payload = run_job(tmp_path)

    assert payload["status"] == PARTIAL_FAILED
    assert payload["result"]["failure"]["code"] == CODE_OUTPUT_INVALID
    assert payload["error"] is None
    assert not (tmp_path / "runtime" / "judge-diagnostics").exists()


# ---------------------------------------------------------------- 回归护栏


def test_regular_mock_scenario_still_completes(tmp_path: Path) -> None:
    """非强制失败记录不受影响：模拟模式仍给出标签。"""
    payload = run_job(tmp_path, record=make_record(record_id="6"))
    result = payload["result"]

    assert payload["status"] == COMPLETED
    assert result["label"] in {"回答正确", "检索到正确资料但回答错误", "未检索到正确资料"}
    assert result["failure"] is None
    assert all(item["raw_output"] is None for item in result["attempts"])
