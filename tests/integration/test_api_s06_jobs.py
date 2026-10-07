"""S06-02 的任务查询接口测试：``GET /api/jobs/{job_id}`` 落到持久化任务。

同时守住「``POST /api/judge`` 已删除」这一条：判一条由整批取代，首页不再有
进程内任务可查（S06 阶段文档 §0.2 第 1 项、§0.3 第 9 项）。

字段以 [plan/08 §7](../../../plan/08-API接口与数据合同.md) 为准；``mode`` 是
``initial``／``resume``／``retry_failed``／``automatic``／``manual``，**不是**
模拟/真实。全部走本地 SQLite 与模拟客户端，**零真实模型调用**。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_api_s03 import make_config
from test_api_s04 import create_run
from test_api_s04_reads import run_worker_once, upload_sample

from aidhu_om_agent.api.app import create_app

#: plan/08 §7 的任务字段（``worker_id``／``last_activity_at`` 属详情页既有扩展字段）。
PLAN_FIELDS = {
    "job_id",
    "run_id",
    "kind",
    "mode",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "current_record_key",
    "current_stage",
    "error",
    "result",
}


def new_key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def app_config(tmp_path: Path):
    return make_config(tmp_path)


@pytest.fixture()
def client(app_config) -> TestClient:
    return TestClient(create_app(app_config))


def start_run(client: TestClient, tmp_path: Path) -> tuple[str, str]:
    """建一个整批任务，返回 (run_id, job_id)。"""
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    created = create_run(client, validation_id, new_key()).json()["data"]
    return created["run_id"], created["job_id"]


def test_job_is_readable_before_the_worker_starts(
    client: TestClient, tmp_path: Path
) -> None:
    """**关键回归**：S03 的内存注册表查不到落库任务——这条以前必然 404。"""
    run_id, job_id = start_run(client, tmp_path)

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert PLAN_FIELDS <= set(data)
    assert data["job_id"] == job_id
    assert data["run_id"] == run_id  # 单独查任务也要能看出属于哪个批次
    assert data["kind"] == "classify"
    assert data["mode"] == "initial"  # 判别任务的做法，不是 mock/real
    assert data["status"] == "queued"
    assert data["started_at"] is None and data["finished_at"] is None
    assert data["current_record_key"] is None and data["current_stage"] is None
    assert data["error"] is None and data["result"] is None


def test_job_matches_the_batch_detail_entry(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """与批次详情的 `recent_jobs` 逐字段一致：两处共用同一个形状，不会漂移。"""
    run_id, job_id = start_run(client, tmp_path)
    run_worker_once(app_config)

    detail = client.get(f"/api/runs/{run_id}").json()["data"]
    entry = next(job for job in detail["recent_jobs"] if job["job_id"] == job_id)
    job = client.get(f"/api/jobs/{job_id}").json()["data"]

    assert job == entry
    assert job["status"] == "completed"
    assert job["started_at"] and job["finished_at"]
    assert job["result"]["counts"]["completed"] == 5
    assert job["result"]["attempted"] == 5


def test_job_is_readable_from_a_fresh_app_instance(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """换一个 API 进程（新的 app 实例、空的内存）照样查得到——这正是本条改动的目的。"""
    _, job_id = start_run(client, tmp_path)
    run_worker_once(app_config)

    fresh = TestClient(create_app(app_config))
    response = fresh.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["data"]["job_id"] == job_id


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/api/jobs/" + "0" * 32)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "0" * 32 in body["error"]["message"]
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_judge_endpoint_is_gone(client: TestClient) -> None:
    """`POST /api/judge` 已删除：不能再有第二条判别入口。

    这里断言的是 **404 路由不存在**，不是业务错误码——测试配置没有构建产物，
    因此不挂 SPA 回落，未注册路径由框架直接 404（挂了回落时是
    `NOT_FOUND` 信封，见 [app.py] 的 `spa_fallback`）。两种都不是 405/422：
    说明路由真的没了，而不是「方法不允许」或「参数不合法」。
    """
    response = client.post("/api/judge", json={"mode": "mock", "record": {}})

    assert response.status_code == 404
    assert "data" not in response.json()  # 绝不是成功信封


def test_job_response_carries_no_prompt_or_credential(
    tmp_path: Path,
) -> None:
    """任务字段只报状态与标识，不含提示词正文、模型输入与本机路径。"""
    app_config = make_config(tmp_path, with_credentials=True)
    client = TestClient(create_app(app_config))
    run_id, job_id = start_run(client, tmp_path)
    run_worker_once(app_config)

    body = client.get(f"/api/jobs/{job_id}").text

    assert "test-key" not in body
    assert "阶段一" not in body and "ref1" not in body
    assert "config.toml" not in body
    assert run_id in body  # 冗余提示：确实查的是这个批次的任务
