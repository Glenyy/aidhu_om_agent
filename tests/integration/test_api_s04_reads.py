"""S04-07 的接口集成测试：**批次列表与详情**、以及前端深链接回落。

前置（创建批次、恢复）在 `test_api_s04.py`，这里只测读接口。全部走本地 SQLite、
合成样例与模拟客户端，**零真实模型调用**。

对照 [plan/08 §5](../../../plan/08-API接口与数据合同.md) 核对字段、错误码与状态枚举；
`plan/08 §1` 要求所有错误都用 ``error`` + ``request_id`` 信封——包括参数校验失败，
所以这里也守住「同一个 422 只有一种形状」。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.excel_samples import normal_workbook
from test_api_s03 import make_config, prepared_validation
from test_api_s04 import create_run

from aidhu_om_agent.agent.mock_samples import MockClient
from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.excel.samples import FORCED_FAILURE_RECORD_ID, build_workbook_bytes, get_sample
from aidhu_om_agent.services.batches import RUN_STATUSES
from aidhu_om_agent.storage import Database
from aidhu_om_agent.worker import MODE_MOCK, Worker


def auto_factory(record) -> MockClient:  # noqa: ANN001 - 只用于构造模拟客户端
    """按记录自身的情境构造模拟客户端。

    **不**统一改成「回答正确」：样例的组成决定它宣称的结果——资料全空的记录
    判不出「回答正确」，混合样例里那条确定性技术失败也必须仍然是失败。统一情境
    会把样例变成一个测试作者都没想到的批次。
    """
    return MockClient(record.to_qa_record())


@pytest.fixture()
def app_config(tmp_path: Path):
    return make_config(tmp_path)


@pytest.fixture()
def client(app_config) -> TestClient:
    return TestClient(create_app(app_config))


def new_key() -> str:
    return uuid.uuid4().hex


def upload_sample(client: TestClient, tmp_path: Path, name: str) -> str:
    """上传一份内置合成样例并预检通过，返回 ``validation_id``。"""
    sample = get_sample(name)
    assert sample is not None, f"样例不存在：{name}"
    path = tmp_path / "samples" / sample.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_workbook_bytes(sample))
    validation_id, _ = prepared_validation(client, path)
    return validation_id


def run_worker_once(config, *, client_factory=None) -> None:
    """用模拟客户端把队列跑空（零真实调用）。"""
    worker = Worker(
        config,
        mode=MODE_MOCK,
        poll_seconds=0.01,
        sleep=lambda _: None,
        client_factory=client_factory or auto_factory,
    )
    worker.start()
    try:
        worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()


# ---------------------------------------------------------------- 批次列表


def test_run_list_is_empty_before_any_batch(client: TestClient) -> None:
    response = client.get("/api/runs")

    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "page": 1, "page_size": 50, "total": 0}


def test_run_list_shows_the_created_batch(client: TestClient, tmp_path: Path) -> None:
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    response = client.get("/api/runs")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1 and len(data["items"]) == 1
    item = data["items"][0]
    assert item["run_id"] == run_id
    assert item["original_filename"] == "synthetic-five-scenarios.xlsx"
    assert item["status"] == "queued"
    assert item["revision"] == 0
    # 还没跑：processed = classified + failed + input_invalid = 0，remaining = total。
    assert item["counts"]["total"] == 5
    assert item["counts"]["processed"] == 0
    assert item["counts"]["remaining"] == 5
    assert item["progress_percent"] == 0.0
    assert item["started_at"] is None and item["finished_at"] is None
    assert item["created_at"]


def test_run_list_orders_newest_first(client: TestClient, app_config, tmp_path: Path) -> None:
    """排序 `created_at DESC, run_id DESC`；同一秒内建成时把一条改到更早才有确定答案。"""
    first = create_run(client, upload_sample(client, tmp_path, "no-refs"), new_key()).json()["data"][
        "run_id"
    ]
    second = create_run(client, upload_sample(client, tmp_path, "no-refs"), new_key()).json()["data"][
        "run_id"
    ]
    connection = Database(app_config.paths.database).connect()
    try:
        connection.execute(
            "UPDATE runs SET created_at = '2020-01-01T00:00:00+00:00' WHERE run_id = ?",
            (second,),
        )
        connection.commit()
    finally:
        connection.close()

    items = client.get("/api/runs").json()["data"]["items"]

    assert [item["run_id"] for item in items] == [first, second]


def test_run_list_pages_and_reports_the_filtered_total(client: TestClient, tmp_path: Path) -> None:
    validation_id = upload_sample(client, tmp_path, "no-refs")
    for _ in range(3):
        create_run(client, validation_id, new_key())

    data = client.get("/api/runs", params={"page": 2, "page_size": 2}).json()["data"]

    assert (data["page"], data["page_size"], data["total"]) == (2, 2, 3)
    assert len(data["items"]) == 1


def test_run_list_filters_by_status(client: TestClient, app_config, tmp_path: Path) -> None:
    validation_id = upload_sample(client, tmp_path, "no-refs")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    run_worker_once(app_config)

    queued = client.get("/api/runs", params={"status": "queued"}).json()["data"]
    completed = client.get("/api/runs", params={"status": "completed"}).json()["data"]

    assert queued["total"] == 0 and queued["items"] == []
    assert completed["total"] == 1
    assert completed["items"][0]["run_id"] == run_id


def test_run_list_rejects_an_unknown_status(client: TestClient) -> None:
    response = client.get("/api/runs", params={"status": "已经完成"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "PARAM_VALIDATION"
    assert error["details"]["allowed"] == sorted(RUN_STATUSES)


def test_run_list_rejects_out_of_range_paging(client: TestClient) -> None:
    """越界分页与非法状态必须用**同一种**错误信封，不能一个 error 一个 detail。"""
    for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        response = client.get("/api/runs", params=params)
        assert response.status_code == 422, params
        body = response.json()
        assert body["error"]["code"] == "PARAM_VALIDATION", params
        assert body["request_id"], params


# ---------------------------------------------------------------- 批次详情


def test_run_detail_reports_the_full_contract(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    run_worker_once(app_config)

    response = client.get(f"/api/runs/{run_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    # plan/08 §5 的详情字段一个不少。
    assert {
        "run_id",
        "original_filename",
        "sheet_name",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "revision",
        "counts",
        "progress_percent",
        "active_job",
        "execution_control",
        "model_config",
        "versions",
        "last_error",
        "allowed_actions",
        "latest_export",
    } <= set(data)
    assert data["run_id"] == run_id
    assert data["status"] == "completed"
    assert data["started_at"] and data["finished_at"]
    # 5 条全部判完：五类模拟情境都能过真实校验，所以 classified = total。
    assert data["counts"]["classified"] == 5
    assert data["counts"]["processed"] == 5
    assert data["counts"]["remaining"] == 0
    assert data["progress_percent"] == 100.0
    assert data["counts"]["review_required"] >= 1  # forced_review 情境
    # 批次结束后没有活跃任务，但最近任务仍在：界面靠它显示上一轮的结局与错误。
    # 最近在前：终态时排上的自动导出（S05-03）排在判别任务之前。
    assert data["active_job"] is None
    assert len(data["recent_jobs"]) == 2
    assert data["recent_jobs"][0]["kind"] == "export"
    assert data["recent_jobs"][0]["mode"] == "automatic"
    assert data["recent_jobs"][0]["status"] == "completed"
    assert data["recent_jobs"][1]["kind"] == "classify"
    assert data["recent_jobs"][1]["mode"] == "initial"
    assert data["recent_jobs"][1]["status"] == "completed"
    # 导出概况自 S05-04 起是真值：终态那次自动导出已经被 worker 跑完，两份文件登记
    # 齐全。捕获信息与该次导出的登记一致，不是拿 scheduled_revision 冒充。
    export = data["latest_export"]
    assert export is not None
    assert export["source"] == "automatic"
    assert export["job_id"] == data["recent_jobs"][0]["job_id"]
    assert export["job_status"] == "completed"
    assert export["captured_at"] and export["run_status_at_capture"] == "completed"
    assert export["run_revision"] == data["revision"]
    assert export["counts_at_capture"]["remaining"] == 0
    assert export["error"] is None
    assert [item["kind"] for item in export["artifacts"]] == ["excel", "jsonl"]
    for item in export["artifacts"]:
        assert item["download_url"] == f"/api/artifacts/{item['artifact_id']}/download"
        assert item["download_name"].endswith(
            ".xlsx" if item["kind"] == "excel" else ".jsonl"
        )
    # 导出在飞也可以导出（S05-04）：这里导出已结束，按钮回到可用。
    assert data["allowed_actions"]["can_export"] is True
    assert data["allowed_actions"]["export_disabled_reason"] is None
    assert data["last_error"] is None


def test_run_detail_call_statistics_are_reported_per_stage(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """调用统计要能被手动指南当证据用：恢复前后对比阶段一的数字不增长。"""
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    run_worker_once(app_config)

    data = client.get(f"/api/runs/{run_id}").json()["data"]

    assert data["call_statistics"]["stage1"] == {
        "attempts": 5,
        "succeeded": 5,
        "failed": 0,
        "unknown_after_interrupt": 0,
        "simulated": 5,
    }
    assert data["call_statistics"]["stage2"]["attempts"] == 5
    assert data["call_statistics"]["stage2"]["simulated"] == 5


def test_run_detail_partial_failure_lists_the_failed_record(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """混合样例：1 条确定性技术失败 + 2 条正常 → partial_failed，失败项可重试。"""
    validation_id = upload_sample(client, tmp_path, "mixed-outcome")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    run_worker_once(app_config)

    data = client.get(f"/api/runs/{run_id}").json()["data"]

    assert data["status"] == "partial_failed"
    assert data["counts"]["classified"] == 2
    assert data["counts"]["failed"] == 1
    assert data["counts"]["processed"] == 3
    assert data["counts"]["remaining"] == 0
    summary = data["failure_summary"]
    assert summary["count"] == 1 and summary["truncated"] is False
    failure = summary["items"][0]
    assert failure["record_id"] == FORCED_FAILURE_RECORD_ID
    assert failure["failure_stage"] == "stage2"
    assert failure["code"] == "OUTPUT_INVALID"
    # 这条失败把阶段二的 3 次预算用满了（`max_attempts_per_stage_campaign`），所以
    # 「本预算内还能重试」为否、普通恢复跳不过去——界面因此要给出**重开预算**的
    # 「重试失败项」按钮，而不是让用户以为恢复一下就好了。
    assert failure["attempt_count"] == 3
    assert failure["retryable"] is False
    assert data["allowed_actions"]["can_resume"] is False
    assert data["allowed_actions"]["skipped_budget_exhausted"] == 1
    # 有失败可重试：allowed_actions 与提交路径同源，界面不会出现「按钮可点却被拒」。
    assert data["allowed_actions"]["can_retry_failed"] is True
    assert data["allowed_actions"]["retry_failed_selected"] == 1
    # `last_error` 在这里是**批次收尾摘要**（为什么不是 completed），不是「批次
    # 执行不下去」：后者是系统性故障，会带暂停派发标志。
    assert data["last_error"] == {"code": "PARTIAL_FAILED", "failed_count": 1}
    assert data["execution_control"]["model_dispatch_paused"] is False


def test_run_detail_exposes_worker_registration_without_judging_liveness(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """worker 状态只报「最近登记」，不提供任何「进程是否还活着」的推论字段。"""
    validation_id = upload_sample(client, tmp_path, "no-refs")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    before = client.get(f"/api/runs/{run_id}").json()["data"]["execution_control"]
    assert before["last_worker"] is None
    assert before["model_dispatch_paused"] is False

    run_worker_once(app_config)

    after = client.get(f"/api/runs/{run_id}").json()["data"]["execution_control"]
    assert after["last_worker"]["mode"] == "mock"
    assert after["last_worker"]["worker_id"]
    assert after["runtime_updated_at"]


def test_run_detail_returns_not_found_for_unknown_run(client: TestClient) -> None:
    response = client.get("/api/runs/" + "0" * 32)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_run_detail_does_not_leak_prompts_or_credentials(tmp_path: Path) -> None:
    """详情含模型配置快照，但**不含**提示词正文、凭据值与本机路径（[plan/08 §5]）。

    这一条用**配了凭据**的配置跑：没有凭据的配置即使泄漏了也看不出来。
    """
    app_config = make_config(tmp_path, with_credentials=True)
    client = TestClient(create_app(app_config))
    validation_id = upload_sample(client, tmp_path, "five-scenarios")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]
    run_worker_once(app_config)

    response = client.get(f"/api/runs/{run_id}")

    assert response.status_code == 200
    body = response.text
    assert "test-key" not in body  # 凭据值不出现
    assert "阶段一" not in body and "ref1" not in body  # 提示词正文不出现
    assert "config.toml" not in body  # 来源配置路径不出现

    data = response.json()["data"]
    assert set(data["model_config"]) == {"stage1", "stage2", "execution"}
    model_keys = {"base_url", "model", "timeout_seconds", "api_key_present"}
    assert set(data["model_config"]["stage1"]) == model_keys
    assert set(data["model_config"]["stage2"]) == model_keys
    # 凭据**只以「是否存在」的形式**出现：这里给了凭据，所以是 true，但值不在其中。
    assert data["model_config"]["stage1"]["api_key_present"] is True
    assert set(data["model_config"]["execution"]) == {
        "concurrency",
        "max_attempts_per_stage_campaign",
        "retry_backoff_seconds",
    }


# ------------------------------------------------ 中断与恢复（S04-07 手动流程）


class KillSwitch(BaseException):
    """模拟进程被杀：**不是** `Exception`，免得被 worker 的错误分支当成单条失败。

    与 `tests/unit/backend/test_interruptions.py` 的故障注入同类；那里在**连接层**
    注入以覆盖五种中断点，这里只需要「阶段二调用到一半被杀」这一种。
    """


class CalledThenKilled:
    """包装模拟客户端：第 ``kill_at`` 次调用时抛出 `KillSwitch`。"""

    def __init__(self, inner: MockClient, *, kill_at: int, counter: list[int]) -> None:
        self._inner = inner
        self._kill_at = kill_at
        self._counter = counter

    def call(self, messages, stage):  # noqa: ANN001, ANN201 - 测试替身
        self._counter.append(1)
        if len(self._counter) == self._kill_at:
            raise KillSwitch(f"第 {self._kill_at} 次调用时被杀")
        return self._inner.call(messages, stage)


def test_resume_does_not_repeat_stage1_for_a_committed_record(tmp_path: Path) -> None:
    """手动指南的核对点：恢复只补没做完的，**已提交的阶段一不被重复调用**。

    慢速样例 3 条，在第 2 次调用（记录 1 的阶段二）中途杀进程：记录 1 的阶段一
    结果已提交，占位中的阶段二变成「中断未知」。恢复跑完后阶段一尝试数应**正好
    等于记录数 3**；若记录 1 的阶段一被重跑，这里会是 4。
    """
    app_config = make_config(tmp_path)
    client = TestClient(create_app(app_config))
    validation_id = upload_sample(client, tmp_path, "slow-batch")
    run_id = create_run(client, validation_id, new_key()).json()["data"]["run_id"]

    counter: list[int] = []

    def killing_factory(record):  # noqa: ANN001 - 测试替身
        # 慢速样例真的会 sleep；这里关掉等待，只保留它「一条条慢慢做」的形态。
        inner = MockClient(record.to_qa_record(), sleep=lambda _: None)
        return CalledThenKilled(inner, kill_at=2, counter=counter)

    worker = Worker(
        app_config, mode=MODE_MOCK, poll_seconds=0.01, sleep=lambda _: None,
        client_factory=killing_factory,
    )
    worker.start()
    try:
        with pytest.raises(KillSwitch):
            worker.run_forever(max_idle_rounds=1)
    finally:
        worker.stop()

    killed = client.get(f"/api/runs/{run_id}").json()["data"]
    assert killed["status"] == "running"  # 进程死了，状态还停在运行中
    assert killed["call_statistics"]["stage1"]["attempts"] == 1
    assert killed["call_statistics"]["stage2"]["attempts"] == 1

    # 新 worker 启动：启动恢复把 running 的调用改成「中断未知」，批次改为 interrupted。
    recovering = Worker(app_config, mode=MODE_MOCK, poll_seconds=0.01, sleep=lambda _: None)
    recovering.start()
    recovering.stop()

    interrupted = client.get(f"/api/runs/{run_id}").json()["data"]
    assert interrupted["status"] == "interrupted"
    assert interrupted["call_statistics"]["stage2"]["unknown_after_interrupt"] == 1
    assert interrupted["allowed_actions"]["can_resume"] is True

    # 恢复入队 + 新 worker 跑完。
    resumed = client.post(
        f"/api/runs/{run_id}/resume", json={}, headers={"Idempotency-Key": new_key()}
    )
    assert resumed.status_code == 202
    assert resumed.json()["data"]["selected_records"] == 3
    run_worker_once(app_config)

    final = client.get(f"/api/runs/{run_id}").json()["data"]
    assert final["status"] == "completed"
    assert final["counts"]["classified"] == 3
    stats = final["call_statistics"]
    # 这就是界面上的核对点：阶段一 = 记录数，说明没有重复调用。
    assert stats["stage1"]["attempts"] == 3
    assert stats["stage2"]["attempts"] == 4  # 3 条 + 1 次中断未知的补跑
    assert stats["stage2"]["unknown_after_interrupt"] == 1


# ------------------------------------ 服务重启后的界面深链接（S04-07 完成条件）


def build_frontend(tmp_path: Path, *, index: bool = True):
    """造一个最小的构建产物目录，返回 (config, client)。"""
    config = make_config(tmp_path)
    dist = config.paths.frontend_dist
    dist.mkdir(parents=True, exist_ok=True)
    if index:
        (dist / "index.html").write_text("<html>marker</html>", encoding="utf-8")
        (dist / "assets").mkdir(exist_ok=True)
        (dist / "assets" / "app.js").write_text("//x", encoding="utf-8")
    return config, TestClient(create_app(config))


def test_frontend_serves_index_for_history_deep_links(tmp_path: Path) -> None:
    """刷新 ``/runs/{run_id}`` 必须仍能打开页面：history 模式要靠后端回落。"""
    _, client = build_frontend(tmp_path)

    deep = client.get("/runs/" + "a" * 32)

    assert deep.status_code == 200 and "marker" in deep.text
    assert client.get("/").text == "<html>marker</html>"
    # 真实静态文件按文件返回，不走回落。
    assert client.get("/assets/app.js").text == "//x"


def test_frontend_does_not_swallow_api_404s(tmp_path: Path) -> None:
    """拼错的接口路径必须得到 JSON 错误，不能回落成 200 + HTML。"""
    _, client = build_frontend(tmp_path)

    response = client.get("/api/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert "marker" not in response.text


def test_frontend_dist_without_index_reports_a_build_problem(tmp_path: Path) -> None:
    """产物目录在但没 index.html：报部署问题，不要假装「页面不存在」。"""
    _, client = build_frontend(tmp_path, index=False)

    response = client.get("/runs/abc")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_uploads_still_work_with_frontend_mounted(tmp_path: Path) -> None:
    """挂上前端回落之后，``/api`` 路由优先于通配回落（顺序错会全部变成 HTML）。"""
    _, client = build_frontend(tmp_path)

    upload = client.post(
        "/api/uploads",
        files={
            "file": (
                "ok.xlsx",
                normal_workbook(tmp_path / "ok.xlsx").read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert upload.status_code == 201
    assert upload.json()["data"]["original_filename"] == "ok.xlsx"
