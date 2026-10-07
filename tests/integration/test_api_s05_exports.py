"""S05-04 的接口集成测试：**手动导出、导出历史与文件下载**。

覆盖 [plan/08 §7] 的三条接口与 `run_detail` 的导出字段：

- ``POST /api/runs/{run_id}/exports``：空对象请求、幂等键、只入队不生成文件、
  已有导出在飞时 409；
- ``GET /api/runs/{run_id}/exports``：新→旧、含失败与未完成的导出；
- ``GET /api/artifacts/{artifact_id}/download``：按 `artifact_id` 定位登记文件、
  文件名与字节可核对、文件没了返回 404 `ARTIFACT_MISSING` 而**不是空文件**；
- `run_detail` 的 `latest_export` 与 `allowed_actions.can_export`。

全程模拟模式、本地 SQLite、临时 `outputs/`，**零真实模型调用**。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from test_api_s03 import make_config, prepared_validation
from test_api_s04 import create_run
from test_api_s04_reads import new_key, run_worker_once, upload_sample

from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.llm.client import ModelConfigError


class AuthFailureClient:
    """一调用就报配置无效（认证类故障）的客户端；用来把批次推到 `failed`。"""

    def __init__(self, record) -> None:  # noqa: ANN001 - 只用于抛错
        self.record = record

    def call(self, messages, stage):  # noqa: ANN001, ANN201 - 只用于抛错
        raise ModelConfigError("缺少服务地址或凭据", stage=stage)


@pytest.fixture()
def app_config(tmp_path: Path):
    return make_config(tmp_path)


@pytest.fixture()
def client(app_config) -> TestClient:
    return TestClient(create_app(app_config))


def post_export(client: TestClient, run_id: str, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/runs/{run_id}/exports", headers=headers)


def history(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}/exports")
    assert response.status_code == 200
    return response.json()["data"]


def detail(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    return response.json()["data"]


def completed_run(client: TestClient, config, tmp_path: Path, name: str = "five-scenarios") -> str:
    """建一个跑完的批次（判别 + 终态那次自动导出都跑完）。"""
    validation_id = upload_sample(client, tmp_path, name)
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    run_worker_once(config)
    return run_id


def export_rows(config) -> list[sqlite3.Row]:
    connection = sqlite3.connect(config.paths.database)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                "SELECT e.*, j.status AS job_status, j.mode AS job_mode"
                " FROM exports e JOIN jobs j ON j.job_id = e.job_id"
                " ORDER BY e.rowid"
            )
        )
    finally:
        connection.close()


def write_sql(config, statement: str, parameters: tuple = ()) -> None:
    """直接改库：用来构造接口本身造不出来的状态（如产物行指向根目录之外）。"""
    connection = sqlite3.connect(config.paths.database)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


# ------------------------------------------------------------ 入队（POST）


def test_export_needs_an_idempotency_key(client: TestClient, app_config, tmp_path: Path) -> None:
    run_id = completed_run(client, app_config, tmp_path)

    response = post_export(client, run_id)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "PARAM_VALIDATION"
    assert body["request_id"]
    # 少键就是没排：不能出现「报错了但导出还是排上了」。
    assert len(export_rows(app_config)) == 1  # 只有终态那次自动导出


def test_manual_export_is_queued_and_not_generated_yet(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = completed_run(client, app_config, tmp_path)
    revision = detail(client, run_id)["revision"]

    response = post_export(client, run_id, new_key())

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["reused"] is False
    assert data["export_id"] and data["job_id"]

    rows = export_rows(app_config)
    manual = rows[-1]
    assert manual["export_id"] == data["export_id"]
    assert manual["source"] == "manual"
    # 手动导出记的是**排队时**的 revision；自动导出去重只看 source='automatic'。
    assert manual["scheduled_revision"] == revision
    # 还没被 worker 认领：快照未捕获、没有文件。
    assert manual["captured_at"] is None and manual["captured_revision"] is None
    assert manual["job_status"] == "queued" and manual["job_mode"] == "manual"

    # 详情里看到的是这条排队中的导出，且按钮被挡住（规则与后端提交一致）。
    payload = detail(client, run_id)
    assert payload["latest_export"]["export_id"] == data["export_id"]
    assert payload["latest_export"]["job_status"] == "queued"
    assert payload["latest_export"]["captured_at"] is None
    assert payload["latest_export"]["run_revision"] is None
    assert payload["latest_export"]["counts_at_capture"] is None
    assert payload["latest_export"]["artifacts"] == []
    assert payload["allowed_actions"]["can_export"] is False
    assert "导出任务" in payload["allowed_actions"]["export_disabled_reason"]

    history_items = history(client, run_id)["items"]
    assert history_items[0]["export_id"] == data["export_id"]
    assert history_items[0]["job_status"] == "queued"


def test_the_same_key_replays_the_same_export(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = completed_run(client, app_config, tmp_path)
    key = new_key()

    first = post_export(client, run_id, key)
    second = post_export(client, run_id, key)

    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["reused"] is False
    assert second.json()["data"]["reused"] is True
    # 重放拿到的是第一次那份，只是 `reused` 如实标成真。
    assert first.json()["data"]["export_id"] == second.json()["data"]["export_id"]
    assert first.json()["data"]["job_id"] == second.json()["data"]["job_id"]
    # 只排了一份：自动 1 + 手动 1。
    assert [row["source"] for row in export_rows(app_config)] == ["automatic", "manual"]


def test_a_second_export_is_rejected_while_one_is_in_flight(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """同一批次同时只允许一份导出在飞；换一个幂等键也一样被挡住。"""
    run_id = completed_run(client, app_config, tmp_path)
    first = post_export(client, run_id, new_key())
    assert first.status_code == 202

    response = post_export(client, run_id, new_key())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATE_CONFLICT"
    assert len(export_rows(app_config)) == 2  # 被拒的那次没留下任何行


def test_export_is_allowed_before_the_batch_is_judged(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """任意批次状态都能导出（[plan/08 §7]）：还没跑就导，得到的是「未处理」快照。"""
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    assert detail(client, run_id)["status"] == "queued"
    assert detail(client, run_id)["allowed_actions"]["can_export"] is True

    assert post_export(client, run_id, new_key()).status_code == 202

    run_worker_once(app_config)  # 判别任务被闸门/顺序放行后跑到终态

    items = history(client, run_id)["items"]
    assert len(items) == 2  # 手动那份 + 终态那份自动导出
    for item in items:
        assert item["job_status"] == "completed"
        assert [artifact["kind"] for artifact in item["artifacts"]] == ["excel", "jsonl"]


def test_a_failed_batch_accepts_a_manual_export(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """系统性故障的批次**不排**自动导出（S05-03），但用户仍可手动导出一份。"""
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    # 认证类故障：worker 停整批、把批次标成 failed 并暂停判别派发。
    run_worker_once(app_config, client_factory=AuthFailureClient)
    assert detail(client, run_id)["status"] == "failed"
    assert export_rows(app_config) == []

    assert post_export(client, run_id, new_key()).status_code == 202
    # 新的 worker 启动会解除暂停，导出任务照常被消费（闸门只管模型派发）。
    run_worker_once(app_config)

    rows = export_rows(app_config)
    assert [row["source"] for row in rows] == ["manual"]
    assert rows[0]["job_status"] == "completed"
    assert rows[0]["captured_run_status"] == "failed"
    counts = json.loads(rows[0]["captured_counts_json"])
    # 未处理的记录数写在捕获计数里，界面据此提示「这不是一份完整成功批次的导出」：
    # 只有第一条真的调用过并失败，其余几条根本没跑。
    assert counts["classified"] == 0
    assert counts["failed"] == 1
    assert counts["remaining"] == counts["total"] - 1 == 4


# ------------------------------------------------------------ 历史（GET）


def test_history_lists_newest_first_with_counts_and_files(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = completed_run(client, app_config, tmp_path)
    post_export(client, run_id, new_key())
    run_worker_once(app_config)

    payload = history(client, run_id)

    assert payload["run_id"] == run_id
    assert payload["total"] == 2 and payload["page"] == 1
    manual, automatic = payload["items"]
    assert (manual["source"], automatic["source"]) == ("manual", "automatic")
    assert manual["job_status"] == automatic["job_status"] == "completed"
    # 与该批次终态一致：五条全判完，未处理数为 0。
    assert manual["counts_at_capture"]["remaining"] == 0
    assert manual["run_status_at_capture"] == "completed"
    assert manual["captured_at"] and manual["run_revision"] == detail(client, run_id)["revision"]
    assert manual["error"] is None
    for artifact in manual["artifacts"]:
        assert artifact["size_bytes"] > 0
        assert len(artifact["sha256"]) == 64
        assert artifact["download_url"].startswith("/api/artifacts/")
    # 与详情里的「最近一次导出」是同一份。
    assert detail(client, run_id)["latest_export"]["export_id"] == manual["export_id"]


def test_history_reports_a_failed_export_without_files(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """导出失败要留在历史上：状态 failed、原因可读、没有文件、批次不受影响。

    失败用「目标目录被一个同名文件占住」造出来——这是 worker 真会遇到的磁盘问题，
    不靠改库伪造错误。
    """
    run_id = completed_run(client, app_config, tmp_path)
    created = post_export(client, run_id, new_key()).json()["data"]
    blocked = app_config.paths.outputs / run_id / created["export_id"]
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("占位文件", encoding="utf-8")

    run_worker_once(app_config)

    items = history(client, run_id)["items"]
    failed = next(item for item in items if item["export_id"] == created["export_id"])
    assert failed["job_status"] == "failed"
    assert failed["artifacts"] == []
    assert failed["error"]["code"] == "EXPORT_FAILED"
    # 原因里不带本机路径：这条会被原样发给浏览器（[plan/08 §1]）。
    # **三种写法都要查**：`OSError.__str__` 用 `repr()` 拼 `filename`，Windows
    # 路径的反斜杠在消息里是**两个字符**；只查 `str(path)` 会假阴性通过。
    message = failed["error"]["message"]
    root = str(app_config.paths.outputs)
    assert root not in message, message
    assert root.replace("\\", "\\\\") not in message, message
    assert root.replace("\\", "/") not in message, message
    # 路径片段（批次 id 与导出 id 只作为目录名出现）同样不该留下。
    assert run_id not in message and created["export_id"] not in message, message
    # 失败的只是导出：分类结果与批次状态原封不动。
    assert detail(client, run_id)["status"] == "completed"
    assert detail(client, run_id)["counts"]["classified"] == 5
    # 原始文件也没有被当成「半份产物」登记。
    assert detail(client, run_id)["latest_export"]["export_id"] == created["export_id"]
    assert detail(client, run_id)["latest_export"]["job_status"] == "failed"


def test_a_new_export_after_a_failure_gets_its_own_directory(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """失败之后重新导出：新的 export_id、新的文件，原文件与人工改过的文件不受影响。"""
    run_id = completed_run(client, app_config, tmp_path)
    first = post_export(client, run_id, new_key()).json()["data"]
    blocked = app_config.paths.outputs / run_id / first["export_id"]
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("占位文件", encoding="utf-8")
    run_worker_once(app_config)

    second = post_export(client, run_id, new_key()).json()["data"]
    run_worker_once(app_config)

    assert second["export_id"] != first["export_id"]
    items = {item["export_id"]: item for item in history(client, run_id)["items"]}
    assert items[first["export_id"]]["job_status"] == "failed"
    assert items[second["export_id"]]["job_status"] == "completed"
    assert len(items[second["export_id"]]["artifacts"]) == 2
    # 失败那次留下的占位文件还在原处，没有被覆盖或清理。
    assert blocked.read_text(encoding="utf-8") == "占位文件"


def test_history_of_an_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/api/runs/nope/exports")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_export_of_an_unknown_run_is_404(client: TestClient) -> None:
    response = post_export(client, "nope", new_key())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------ 下载（GET）


def test_download_returns_the_registered_bytes(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = completed_run(client, app_config, tmp_path)
    export = detail(client, run_id)["latest_export"]

    for artifact in export["artifacts"]:
        response = client.get(artifact["download_url"])

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(artifact["media_type"])
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert quote(artifact["download_name"]) in disposition
        # 字节数与摘要与登记行一致：下载到的确实是那一份。
        assert len(response.content) == artifact["size_bytes"]
        disk = app_config.paths.outputs / f"{run_id}/{export['export_id']}"
        assert (disk / artifact["download_name"]).read_bytes() == response.content


def test_download_of_an_unknown_artifact_is_404(client: TestClient) -> None:
    response = client.get(f"/api/artifacts/{uuid.uuid4().hex}/download")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_MISSING"


def test_download_is_404_when_the_file_is_gone(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """文件没了就给 404，**不能当空文件下载**：那会把「丢了」读成「导出为空」。"""
    run_id = completed_run(client, app_config, tmp_path)
    export = detail(client, run_id)["latest_export"]
    artifact = export["artifacts"][0]
    disk = app_config.paths.outputs / f"{run_id}/{export['export_id']}"
    (disk / artifact["download_name"]).unlink()

    response = client.get(artifact["download_url"])

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "ARTIFACT_MISSING"
    assert "重新导出" in body["error"]["message"]
    # 另一份仍在，照常可下：一份文件丢失不牵连另一份。
    assert client.get(export["artifacts"][1]["download_url"]).status_code == 200


def test_download_never_serves_a_path_outside_the_outputs_root(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """登记行被改坏也不能读到导出目录之外的文件（接口不接受用户给的路径）。"""
    run_id = completed_run(client, app_config, tmp_path)
    artifact = detail(client, run_id)["latest_export"]["artifacts"][0]
    secret = tmp_path / "secret.txt"
    secret.write_text("不该被下载到的内容", encoding="utf-8")
    write_sql(
        app_config,
        "UPDATE artifacts SET relative_path = ? WHERE artifact_id = ?",
        ("../../secret.txt", artifact["artifact_id"]),
    )

    response = client.get(artifact["download_url"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_MISSING"
    assert "不该被下载到的内容" not in response.text
