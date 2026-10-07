"""S04-02 的接口集成测试：上传 → 预检 → **创建批次**。

全部走本地 SQLite 与合成工作簿，**不产生任何真实模型调用**；创建批次只入队，
不在这里执行模型（worker 属 S04-04/S04-06）。

复用 `tests/integration/test_api_s03.py` 的上传/预检辅助函数，保证新接口与既有
接口在同一套测试配置下一起工作（同一 tmp_path 下的库与上传目录）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.excel_samples import all_invalid_workbook, normal_workbook
from test_api_s03 import make_config, prepared_validation, upload_workbook, validate

from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.repositories import jobs as jobs_repo
from aidhu_om_agent.repositories import runs as runs_repo
from aidhu_om_agent.storage import Database, utc_now


@pytest.fixture()
def app_config(tmp_path: Path):
    return make_config(tmp_path)


@pytest.fixture()
def client(app_config) -> TestClient:
    return TestClient(create_app(app_config))


def new_key() -> str:
    return uuid.uuid4().hex


def create_run(client: TestClient, validation_id: str, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/runs", json={"validation_id": validation_id}, headers=headers)


# ---------------------------------------------------------------- 创建


def test_create_run_returns_ids_and_persists(client: TestClient, app_config, tmp_path: Path) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    response = create_run(client, validation_id, new_key())

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["reused"] is False
    assert len(data["run_id"]) == 32 and len(data["job_id"]) == 32

    database = Database(app_config.paths.database)
    connection = database.connect()
    try:
        run = runs_repo.get_run(connection, data["run_id"])
        assert run is not None
        assert (run.status, run.revision) == ("queued", 0)
        assert (run.total_count, run.valid_count, run.input_invalid_count) == (3, 3, 0)
        assert run.started_at is None
    finally:
        connection.close()


def test_create_run_requires_idempotency_key(client: TestClient, tmp_path: Path) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    response = create_run(client, validation_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PARAM_VALIDATION"


def test_repeated_key_returns_same_run(client: TestClient, tmp_path: Path) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    key = new_key()

    first = create_run(client, validation_id, key)
    second = create_run(client, validation_id, key)

    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    assert second.json()["data"]["reused"] is True
    # request_id 每次新生成，不复用原响应的 request_id。
    assert first.json()["request_id"] != second.json()["request_id"]


def test_same_key_different_body_returns_conflict(client: TestClient, tmp_path: Path) -> None:
    first_id, _ = prepared_validation(client, normal_workbook(tmp_path / "a.xlsx"))
    second_id, _ = prepared_validation(client, normal_workbook(tmp_path / "b.xlsx"))
    key = new_key()
    create_run(client, first_id, key)

    response = create_run(client, second_id, key)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_blocked_validation_is_rejected(client: TestClient, tmp_path: Path) -> None:
    upload = upload_workbook(client, all_invalid_workbook(tmp_path / "bad.xlsx")).json()["data"]
    blocked = validate(client, upload["upload_id"]).json()["data"]
    assert blocked["status"] == "blocked"

    response = create_run(client, blocked["validation_id"], new_key())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INPUT_NOT_VALIDATED"


def test_unknown_validation_returns_not_found(client: TestClient) -> None:
    response = create_run(client, "0" * 32, new_key())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_validation_survives_new_app_instance(client: TestClient, app_config, tmp_path: Path) -> None:
    """服务重启后仍能用原来的 validation_id 创建批次（S04 的完成条件之一）。"""
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))

    restarted = TestClient(create_app(app_config))
    response = create_run(restarted, validation_id, new_key())

    assert response.status_code == 202


# ---------------------------------------------------------------- 恢复（S04-05）


def resume(client: TestClient, run_id: str, *, key: str | None = None, body: dict | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/runs/{run_id}/resume", json=body or {}, headers=headers)


def finish_initial_job(app_config, run_id: str) -> None:
    """把创建批次时排队的初始任务标成已完成，让恢复接口不再被 STATE_CONFLICT 挡住。

    真实流程里这一步由 worker 完成；这里不跑 worker，是为了把接口契约单独测出来。
    """
    connection = Database(app_config.paths.database).connect()
    try:
        connection.execute(
            "UPDATE jobs SET status = 'completed', finished_at = ? WHERE run_id = ?",
            (utc_now(), run_id),
        )
        connection.commit()
    finally:
        connection.close()


def test_resume_requires_idempotency_key(client: TestClient, tmp_path: Path) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    response = resume(client, run_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PARAM_VALIDATION"


def test_resume_enqueues_a_job_and_reports_the_plan(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    finish_initial_job(app_config, run_id)

    response = resume(client, run_id, key=new_key(), body={"retry_failed": False})

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["run_id"] == run_id
    assert len(data["job_id"]) == 32
    assert data["selected_records"] == 3  # 3 条 pending
    assert data["renewed_campaigns"] == 0  # 普通恢复不重开预算
    assert data["skipped_budget_exhausted"] == 0
    assert data["finalize_only"] is False
    assert data["reused"] is False

    database = Database(app_config.paths.database)
    connection = database.connect()
    try:
        run = runs_repo.get_run(connection, run_id)
        assert run is not None
        # 终止时间在恢复入队时清空（plan/08 §5）。
        assert run.status == "queued" and run.finished_at is None
        jobs = jobs_repo.list_jobs_for_run(connection, run_id)
        # 排序键是 `created_at, job_id`，同一秒内建成时 job_id 会决定先后，
        # 所以这里按集合比对，不复用列表顺序。
        assert sorted(job.mode for job in jobs) == ["initial", "resume"]
        resumed_job = jobs_repo.get_job(connection, data["job_id"])
        assert resumed_job is not None
        assert resumed_job.status == "queued"
        assert resumed_job.payload["retry_failed"] is False
        assert len(resumed_job.payload["record_keys"]) == 3
    finally:
        connection.close()


def test_resume_without_work_returns_nothing_to_resume(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    connection = Database(app_config.paths.database).connect()
    try:
        connection.execute(
            "UPDATE jobs SET status = 'completed', finished_at = ? WHERE run_id = ?",
            (utc_now(), run_id),
        )
        connection.execute(
            "UPDATE records SET status = 'completed', final_label = '回答正确',"
            " review_required = 0 WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            "UPDATE runs SET status = 'completed', finished_at = ? WHERE run_id = ?",
            (utc_now(), run_id),
        )
        connection.commit()
    finally:
        connection.close()

    response = resume(client, run_id, key=new_key())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOTHING_TO_RESUME"


def test_resume_conflicts_with_an_active_job(client: TestClient, tmp_path: Path) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    # 刚创建的批次自带 queued 任务：此时恢复必须被拒绝，而不是排出第二个任务。
    response = resume(client, run_id, key=new_key())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATE_CONFLICT"


def test_resume_replays_the_same_key(client: TestClient, app_config, tmp_path: Path) -> None:
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    finish_initial_job(app_config, run_id)
    key = new_key()

    first = resume(client, run_id, key=key)
    second = resume(client, run_id, key=key)

    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["job_id"] == second.json()["data"]["job_id"]
    assert first.json()["data"]["reused"] is False
    assert second.json()["data"]["reused"] is True
    assert first.json()["request_id"] != second.json()["request_id"]


def test_resume_of_unknown_run_returns_not_found(client: TestClient) -> None:
    response = resume(client, "0" * 32, key=new_key())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_resume_does_not_leak_prompt_text(client: TestClient, app_config, tmp_path: Path) -> None:
    """响应里只有标识与计数，没有提示词正文或凭据（[plan/08 §5] 脱敏要求）。"""
    validation_id, _ = prepared_validation(client, normal_workbook(tmp_path / "ok.xlsx"))
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    finish_initial_job(app_config, run_id)

    body = resume(client, run_id, key=new_key()).text

    assert "api_key" not in body and "sk-" not in body
    assert "阶段一" not in body and "ref1" not in body
