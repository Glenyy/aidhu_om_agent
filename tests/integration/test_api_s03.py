"""S03-07 的接口集成测试：上传 → 预检 → 判一条 → 轮询任务。

全部走 ``mock`` 模式，**不产生任何真实模型调用**；真实服务兼容性由 S03-06 的
单独记录证明，不在自动化测试里触发。
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.config import (
    AppConfig,
    ExecutionConfig,
    LimitsConfig,
    ModelConfig,
    PathsConfig,
)
from aidhu_om_agent.excel.reader import PREFERRED_SHEET
from aidhu_om_agent.excel.samples import SAMPLES, get_sample
from aidhu_om_agent.schemas.judgement import LABELS
from fixtures.excel_samples import (
    PREFERRED_SHEET,
    all_invalid_workbook,
    empty_refs_workbook,
    normal_workbook,
    partial_failure_workbook,
)

TERMINAL = {"completed", "partial_failed", "failed"}
POLL_LIMIT = 100


def make_config(
    tmp_path: Path,
    *,
    max_upload_bytes: int = 4 * 1024 * 1024,
    with_credentials: bool = False,
) -> AppConfig:
    """测试配置：路径全部落在 tmp_path，凭据默认为空。"""
    model = ModelConfig(
        base_url="http://127.0.0.1:9/v1" if with_credentials else "",
        model="test-model",
        timeout_seconds=1.0,
        api_key="test-key" if with_credentials else None,
    )
    return AppConfig(
        stage1=model,
        stage2=model,
        execution=ExecutionConfig(
            concurrency=1, max_attempts_per_stage_campaign=3, retry_backoff_seconds=(0, 0)
        ),
        limits=LimitsConfig(max_upload_bytes=max_upload_bytes, max_records=1000),
        paths=PathsConfig(
            database=tmp_path / "runtime" / "db.sqlite3",
            uploads=tmp_path / "runtime" / "uploads",
            runtime=tmp_path / "runtime",
            outputs=tmp_path / "outputs",
            logs=tmp_path / "logs",
            frontend_dist=tmp_path / "dist",  # 不存在：不挂载静态站点
        ),
        source_config=tmp_path / "config.toml",
        source_env=None,
    )


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(make_config(tmp_path)))


def upload_workbook(client: TestClient, path: Path, *, name: str | None = None) -> Any:
    with path.open("rb") as handle:
        return client.post(
            "/api/uploads",
            files={
                "file": (
                    name or path.name,
                    handle.read(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )


def validate(client: TestClient, upload_id: str, sheet: str | None = None) -> Any:
    return client.post(f"/api/uploads/{upload_id}/validate", json={"sheet_name": sheet})


def wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for _ in range(POLL_LIMIT):
        payload = client.get(f"/api/jobs/{job_id}").json()["data"]
        if payload["status"] in TERMINAL:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"任务未在预期内结束：{payload.get('status')}")


def prepared_validation(client: TestClient, path: Path) -> tuple[str, dict[str, Any]]:
    """上传并预检一份通过的样例，返回 (validation_id, payload)。"""
    upload = upload_workbook(client, path).json()["data"]
    response = validate(client, upload["upload_id"])
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "passed"
    return data["validation_id"], data


# ---------------------------------------------------------------- 上传


def test_upload_returns_sheet_catalog_and_digest(client: TestClient, tmp_path: Path) -> None:
    response = upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx"))

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["original_filename"] == "ok.xlsx"
    assert data["size_bytes"] > 0
    assert len(data["sha256"]) == 64 and all(c in "0123456789abcdef" for c in data["sha256"])
    assert data["suggested_sheet"] == PREFERRED_SHEET
    assert [sheet["name"] for sheet in data["sheets"]] == [PREFERRED_SHEET]
    assert data["sheets"][0]["visible"] is True


def test_uploaded_file_is_stored_unmodified(client: TestClient, tmp_path: Path) -> None:
    source = normal_workbook(tmp_path / "ok.xlsx")
    original = source.read_bytes()

    upload_workbook(client, source)

    assert source.read_bytes() == original


def test_every_response_carries_request_id(client: TestClient, tmp_path: Path) -> None:
    response = upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx"))
    body = response.json()

    assert response.headers["X-Request-ID"] == body["request_id"]


def test_non_xlsx_extension_is_rejected(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "qa.csv"
    path.write_text("编号,问题", encoding="utf-8")

    response = upload_workbook(client, path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BAD_WORKBOOK"


def test_unreadable_workbook_is_rejected(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"this is not a workbook")

    response = upload_workbook(client, path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BAD_WORKBOOK"


def test_oversized_upload_is_rejected(tmp_path: Path) -> None:
    client = TestClient(create_app(make_config(tmp_path, max_upload_bytes=64)))

    response = upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx"))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_rejected_upload_leaves_no_file(tmp_path: Path) -> None:
    client = TestClient(create_app(make_config(tmp_path, max_upload_bytes=64)))

    upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx"))

    uploads_dir = tmp_path / "runtime" / "uploads"
    assert not uploads_dir.exists() or not list(uploads_dir.iterdir())


# ---------------------------------------------------------------- 预检


def test_validate_reports_counts_and_records(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx")).json()["data"]

    data = validate(client, upload["upload_id"]).json()["data"]

    assert data["status"] == "passed"
    assert data["sheet_name"] == PREFERRED_SHEET
    assert data["counts"] == {
        "total": 3,
        "valid": 3,
        "input_invalid": 0,
        "skipped_blank_rows": 1,
    }
    assert [record["record_key"] for record in data["records"]] == ["1", "2", "A-003"]
    assert data["records"][0]["q_preview"] == "问题一"
    assert data["records"][0]["ref_count"] == 1
    assert data["records"][1]["ref_count"] == 0
    assert data["file_sha256"] == upload["sha256"]
    assert data["blockers"] == [] and data["row_errors"] == []


def test_each_validation_is_an_independent_snapshot(client: TestClient, tmp_path: Path) -> None:
    path = normal_workbook(tmp_path / "ok.xlsx")
    upload = upload_workbook(client, path).json()["data"]

    first = validate(client, upload["upload_id"]).json()["data"]
    second = validate(client, upload["upload_id"]).json()["data"]

    assert first["validation_id"] != second["validation_id"]  # 每次预检是独立快照
    assert first["counts"] == second["counts"]


def test_validate_with_explicit_sheet_name(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx")).json()["data"]

    data = validate(client, upload["upload_id"], PREFERRED_SHEET).json()["data"]

    assert data["sheet_name"] == PREFERRED_SHEET


def test_validate_reports_row_errors(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, partial_failure_workbook(tmp_path / "partial.xlsx")).json()
    data = validate(client, upload["data"]["upload_id"]).json()["data"]

    assert data["status"] == "passed"
    assert data["counts"]["valid"] == 1
    assert [error["source_row"] for error in data["row_errors"]] == [3, 4, 5]
    assert all(error["reason"] for error in data["row_errors"])
    assert [record["record_key"] for record in data["records"]] == ["1"]


def test_validate_blocks_all_invalid_workbook(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, all_invalid_workbook(tmp_path / "bad.xlsx")).json()["data"]

    data = validate(client, upload["upload_id"]).json()["data"]

    assert data["status"] == "blocked"
    assert data["records"] == []
    assert data["blockers"]  # 批次阻断项必须给出


def test_validate_unknown_upload_returns_404(client: TestClient) -> None:
    response = validate(client, "不存在的上传")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 判一条（模拟）


def test_mock_judge_completes_with_label_and_details(client: TestClient, tmp_path: Path) -> None:
    validation_id, data = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    record_key = data["records"][0]["record_key"]

    created = client.post(
        "/api/judge",
        json={"validation_id": validation_id, "record_key": record_key, "mode": "mock"},
    )

    assert created.status_code == 202
    created_data = created.json()["data"]
    assert created_data["job_id"]
    # 后台线程可能已经开跑，提交响应里的状态不保证仍是 queued。
    assert created_data["status"] in {"queued", "running", "completed"}

    job = wait_for_job(client, created_data["job_id"])

    assert job["status"] == "completed"
    result = job["result"]
    assert result["status"] == "completed"
    assert result["label"] in LABELS
    assert result["simulated"] is True
    assert result["stage1"]["reason"]
    assert result["stage2"]["reason"]
    assert [attempt["stage"] for attempt in result["attempts"]] == ["stage1", "stage2"]
    assert all(attempt["simulated"] for attempt in result["attempts"])


def test_mock_judge_evidence_points_at_real_ref(client: TestClient, tmp_path: Path) -> None:
    validation_id, data = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    created = client.post(
        "/api/judge",
        json={
            "validation_id": validation_id,
            "record_key": data["records"][0]["record_key"],
            "mode": "mock",
        },
    )
    job = wait_for_job(client, created.json()["data"]["job_id"])

    for item in job["result"]["stage1"]["evidence"]:
        assert item["ref_id"].startswith("ref")
        assert item["quote"]


def test_mock_judge_record_without_refs_still_completes(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, empty_refs_workbook(tmp_path / "norefs.xlsx")).json()["data"]
    data = validate(client, upload["upload_id"]).json()["data"]
    assert data["records"][0]["ref_count"] == 0

    created = client.post(
        "/api/judge",
        json={
            "validation_id": data["validation_id"],
            "record_key": data["records"][0]["record_key"],
            "mode": "mock",
        },
    )
    job = wait_for_job(client, created.json()["data"]["job_id"])

    assert job["status"] == "completed"
    assert job["result"]["label"] == "未检索到正确资料"
    assert job["result"]["stage1"]["evidence"] == []


def test_default_mode_is_mock(client: TestClient, tmp_path: Path) -> None:
    validation_id, data = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    created = client.post(
        "/api/judge",
        json={"validation_id": validation_id, "record_key": data["records"][0]["record_key"]},
    )

    assert created.json()["data"]["mode"] == "mock"
    job = wait_for_job(client, created.json()["data"]["job_id"])
    assert job["mode"] == "mock"
    assert job["result"]["simulated"] is True


def test_judge_unknown_validation_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/judge", json={"validation_id": "无", "record_key": "1", "mode": "mock"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_judge_blocked_validation_returns_409(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, all_invalid_workbook(tmp_path / "bad.xlsx")).json()["data"]
    data = validate(client, upload["upload_id"]).json()["data"]

    response = client.post(
        "/api/judge",
        json={"validation_id": data["validation_id"], "record_key": "1", "mode": "mock"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INPUT_NOT_VALIDATED"


def test_judge_unknown_record_key_lists_available_keys(client: TestClient, tmp_path: Path) -> None:
    validation_id, data = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    response = client.post(
        "/api/judge",
        json={"validation_id": validation_id, "record_key": "不存在", "mode": "mock"},
    )

    assert response.status_code == 404
    details = response.json()["error"]["details"]
    assert details["available_record_keys"] == [
        record["record_key"] for record in data["records"]
    ]


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/api/jobs/不存在")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 真实模式的失败面


def test_real_mode_without_credentials_fails_with_config_invalid(tmp_path: Path) -> None:
    """真实模式在缺少服务地址/凭据时必须失败，且不给标签。"""
    client = TestClient(create_app(make_config(tmp_path)))
    validation_id, data = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    created = client.post(
        "/api/judge",
        json={
            "validation_id": validation_id,
            "record_key": data["records"][0]["record_key"],
            "mode": "real",
        },
    )
    job = wait_for_job(client, created.json()["data"]["job_id"])

    assert job["status"] == "partial_failed"
    assert job["result"]["label"] is None
    assert job["result"]["failure"]["code"] == "CONFIG_INVALID"
    assert job["result"]["simulated"] is False


# ---------------------------------------------------------------- 合成样例下载


def test_samples_list_returns_every_definition(client: TestClient) -> None:
    """界面启动时读取的清单：名称、文件名、说明与条数都要可用于展示。"""
    response = client.get("/api/samples")

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["name"] for item in data["samples"]] == [s.name for s in SAMPLES]
    for item, sample in zip(data["samples"], SAMPLES):
        assert item["filename"] == sample.filename
        assert item["description"] == sample.description
        assert item["record_count"] == sample.record_count
        assert item["filename"].endswith(".xlsx")


@pytest.mark.parametrize("name", [sample.name for sample in SAMPLES])
def test_downloaded_sample_can_be_uploaded_and_prechecked(
    client: TestClient, name: str
) -> None:
    """界面上的闭环：下载样例 → 上传 → 预检，计数与样例声明一致。

    ``blocked`` 样例必须真的被阻断，其余样例必须 ``passed``——手动指南里的
    预期结果就是按这个口径写的。
    """
    download = client.get(f"/api/samples/{name}")
    assert download.status_code == 200
    assert download.headers["x-request-id"]
    assert (
        download.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert (
        download.headers["content-disposition"]
        == f'attachment; filename="{get_sample(name).filename}"'  # type: ignore[union-attr]
    )
    assert int(download.headers["content-length"]) == len(download.content)

    upload = client.post(
        "/api/uploads",
        files={
            "file": (
                f"{name}.xlsx",
                download.content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert upload.status_code == 201

    validation = validate(client, upload.json()["data"]["upload_id"])
    assert validation.status_code == 200
    data = validation.json()["data"]
    sample = get_sample(name)
    assert sample is not None
    assert data["counts"]["total"] == sample.record_count
    assert data["status"] == ("blocked" if name == "blocked" else "passed")


def test_download_returns_raw_bytes_without_data_envelope(client: TestClient) -> None:
    """文件下载返回工作簿本体，而不是 JSON 封套；否则浏览器存不下 .xlsx。"""
    response = client.get("/api/samples/five-scenarios")

    assert response.status_code == 200
    assert response.content[:2] == b"PK"  # xlsx 即 zip
    assert b'"data"' not in response.content[:200]
    workbook = load_workbook(BytesIO(response.content))
    assert workbook.sheetnames == [PREFERRED_SHEET]


def test_unknown_sample_returns_404_listing_available_names(client: TestClient) -> None:
    response = client.get("/api/samples/不存在")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    for name in (sample.name for sample in SAMPLES):
        assert name in body["error"]["message"]


# ------------------------------- 2026-10-06 返工：耗时可见与失败界面（模拟、零调用）


def test_terminal_job_payload_carries_timing_fields(
    client: TestClient, tmp_path: Path
) -> None:
    """「四分钟没动静」这类体验问题：界面要能显示已用时长与第几次尝试。"""
    validation_id, data = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    created = client.post(
        "/api/judge",
        json={
            "validation_id": validation_id,
            "record_key": data["records"][0]["record_key"],
            "mode": "mock",
        },
    )
    running = client.get(f"/api/jobs/{created.json()['data']['job_id']}").json()["data"]
    assert "current_attempt" in running  # 轮询期间即可用

    job = wait_for_job(client, created.json()["data"]["job_id"])
    assert job["created_at"] and job["started_at"] and job["finished_at"]
    assert job["created_at"] <= job["started_at"] <= job["finished_at"]
    assert isinstance(job["elapsed_ms"], int) and job["elapsed_ms"] >= 0
    assert job["current_stage"] is None
    assert job["current_attempt"] is None


def test_forced_failure_sample_reaches_failed_job_with_rejected_output(
    client: TestClient, tmp_path: Path
) -> None:
    """界面按下「判一条」即可复现的技术失败：模拟模式、零真实调用。"""
    download = client.get("/api/samples/forced-failure")
    assert download.status_code == 200
    upload = client.post(
        "/api/uploads",
        files={
            "file": (
                "forced-failure.xlsx",
                download.content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert upload.status_code == 201
    data = validate(client, upload.json()["data"]["upload_id"]).json()["data"]
    assert data["status"] == "passed"
    assert [record["record_key"] for record in data["records"]] == ["FF-1"]

    created = client.post(
        "/api/judge",
        json={"validation_id": data["validation_id"], "record_key": "FF-1", "mode": "mock"},
    )
    job = wait_for_job(client, created.json()["data"]["job_id"])
    result = job["result"]

    assert job["status"] == "partial_failed"
    assert result["label"] is None
    assert result["failure"]["code"] == "OUTPUT_INVALID"
    assert result["failure"]["stage"] == "stage2"

    rejected = [item for item in result["attempts"] if item["outcome"] == "validation_error"]
    assert len(rejected) == 3
    assert all(item["raw_output"] for item in rejected)
    assert all(item["raw_output_truncated"] is False for item in rejected)

    # 诊断产物落在 runtime 目录，供事后判读「模型写错了哪个字符」。
    artifact = tmp_path / "runtime" / "judge-diagnostics" / f"judge-{job['job_id']}.json"
    assert artifact.exists()
