"""S07-03 的接口集成测试：**评估结果查询（只读）**。

三个 GET 接口 —— 批次评估历史、评估详情、逐条对照 —— 的形状与边界。要做对的事有
两件，都在这里被钉住：

1. **只读**：网页发起不了评估。`POST /api/runs/{run_id}/evaluations` 必须是错误，
   且**库里不多出评估行**；三个接口都不接受写参数、不发本机绝对路径。
2. **失败不会被说成通过**：校准集「每类 ≥5」不达标时 `passed` 必须是 ``false``，
   `[不可计算]` 项不能算通过。

批次由 `test_evaluation_service.seed_scenario` 造（真实的 上传→预检→建批次→写预测
链路），评估由服务层跑一次，这里只看接口。

全程合成数据、模拟预测，**零真实模型调用**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.evaluation_scenario import (
    CALIBRATION_PER_CLASS,
    CALIBRATION_SIZE,
    WRONG,
    count_rows,
    fail_nth,
    seed_scenario,
    subdir,
)

from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.services.evaluation import EVALUATION_PAGE_SIZE_MAX
from aidhu_om_agent.storage import Database


@pytest.fixture()
def scenario(tmp_path: Path):
    return seed_scenario(tmp_path)


@pytest.fixture()
def client(scenario) -> TestClient:
    """与场景**同一个库**的应用：接口读的就是刚算出来的那份评估。"""
    return TestClient(create_app(scenario.config))


def data_of(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --------------------------------------------------------------- 权限边界


def test_the_page_cannot_start_an_evaluation(client: TestClient, scenario) -> None:
    """**三个接口全是 GET**：网页能看评估，但不能发起评估。

    发起评估是 CLI 的事（[plan/08]：网页不做重活）。这里不只看状态码，还核对
    库里没多出评估行——「返回 405 但其实算了」才是真正要防的。
    """
    database = Database(scenario.config.paths.database)
    before = count_rows(database, "evaluations")

    response = client.post(f"/api/runs/{scenario.run_id}/evaluations")

    assert response.status_code in (404, 405), response.text
    assert "error" in response.json()
    assert count_rows(database, "evaluations") == before


def test_responses_do_not_leak_machine_paths(
    client: TestClient, scenario, tmp_path: Path
) -> None:
    """接口只发文件名与摘要，不发本机绝对路径（api 规则）。"""
    outcome = scenario.evaluate()

    body = client.get(f"/api/evaluations/{outcome.evaluation_id}").text

    assert tmp_path.as_posix() not in body
    detail = json.loads(body)["data"]
    for item in detail["report_files"]:
        assert "/" not in item["name"] and "\\" not in item["name"]
    assert "/" not in detail["gold"]["filename"]


# ----------------------------------------------------------------- 历史


def test_history_is_empty_before_the_first_evaluation(
    client: TestClient, scenario
) -> None:
    """没评过就是空列表，不是 404——「还没评」与「批次不存在」是两回事。"""
    payload = data_of(client.get(f"/api/runs/{scenario.run_id}/evaluations"))

    assert payload["total"] == 0
    assert payload["items"] == []
    assert payload["page"] == 1 and payload["page_size"] == 20


def test_unknown_run_is_not_found(client: TestClient) -> None:
    response = client.get("/api/runs/does-not-exist/evaluations")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_history_lists_each_evaluation_newest_first(
    client: TestClient, scenario
) -> None:
    first = scenario.evaluate()
    second = scenario.evaluate()

    payload = data_of(client.get(f"/api/runs/{scenario.run_id}/evaluations"))

    assert payload["total"] == 2
    assert [item["evaluation_id"] for item in payload["items"]] == [
        second.evaluation_id,
        first.evaluation_id,
    ]
    item = payload["items"][0]
    assert item["split"] == "calibration"
    assert item["scored_count"] == CALIBRATION_SIZE
    assert item["macro_f1"] == "1.0000（1/1）"
    assert item["gold_filename"] == scenario.gold.path.name
    assert len(item["manifest_sha256"]) == 64
    # 校准集 WRONG 只有 3 条，撑不起「每类 ≥5」：整份评估不算通过。
    assert item["passed"] is False
    assert item["report_text_name"].endswith(".md")


# ----------------------------------------------------------------- 详情


def test_detail_carries_the_frozen_numbers(client: TestClient, scenario) -> None:
    outcome = scenario.evaluate()

    detail = data_of(client.get(f"/api/evaluations/{outcome.evaluation_id}"))

    assert detail["split"] == "calibration"
    # 每个指标都带分子分母，不然没法核对「怎么算出来的」。
    macro = detail["metrics"]["macro_f1"]
    assert (macro["numerator"], macro["denominator"]) == (1, 1)
    assert detail["metrics"]["agreement"]["display"] == "1.0000（18/18）"
    assert detail["metrics"]["completion"]["display"] == "1.0000（18/18）"
    assert detail["counts"] == {"valid": 18, "scored": 18, "excluded": 0, "missing": 0}
    assert detail["gold"]["sha256_prefix"] == detail["gold"]["sha256"][:12]
    assert detail["manifest"]["seed"] == 20261005
    assert detail["run"]["run_id"] == scenario.run_id
    assert detail["split_info"]["selected_ids"] == list(scenario.ids)


def test_detail_publishes_the_threshold_verdicts(client: TestClient, scenario) -> None:
    """达标判定逐项给结论；`[不可计算]`/未达标不会被说成通过。"""
    outcome = scenario.evaluate()

    checks = {
        item["key"]: item
        for item in data_of(client.get(f"/api/evaluations/{outcome.evaluation_id}"))[
            "thresholds"
        ]
    }

    assert checks["macro_f1"]["passed"] is True
    assert checks["completion"]["passed"] is True
    assert checks["class_support"]["passed"] is False
    assert checks["class_support"]["name"] == "每类样本数"
    assert "每类 ≥ 5" in checks["class_support"]["requirement"]
    assert checks["class_support"]["actual"] != "全部达标"  # 说清楚差在哪一类


def test_detail_embeds_the_report_for_download(client: TestClient, scenario) -> None:
    """报告内嵌正文 + 文件元信息：页面能直接存成本地文件，不用第四个接口。"""
    outcome = scenario.evaluate()

    detail = data_of(client.get(f"/api/evaluations/{outcome.evaluation_id}"))

    assert detail["report_text"].startswith("# 评估报告")
    kinds = {item["kind"]: item for item in detail["report_files"]}
    assert set(kinds) == {"text", "json"}
    assert all(item["exists"] and item["size_bytes"] > 0 for item in kinds.values())
    assert kinds["text"]["name"] == outcome.report_text_path.name


def test_freeze_list_matches_the_run_snapshot(client: TestClient, scenario) -> None:
    """评估页与批次详情页的模型快照**必须一致**（同一组键、同一个来源）。

    两页各画一遍冻结清单，若各取各的字段，用户会在两处看到「同一批次的不同模型」
    而不知道该信哪个。这条断言就是这次把 `model_config_snapshot` 提为公共函数的理由。
    """
    outcome = scenario.evaluate()

    detail = data_of(client.get(f"/api/evaluations/{outcome.evaluation_id}"))
    run = data_of(client.get(f"/api/runs/{scenario.run_id}"))

    assert detail["freeze"]["model_config"] == run["model_config"]
    assert detail["freeze"]["stage1_prompt_version"] == run["versions"]["stage1_prompt"]
    assert detail["freeze"]["frozen_at"]


def test_unknown_evaluation_is_not_found(client: TestClient) -> None:
    assert client.get("/api/evaluations/0" * 32).status_code == 404
    assert client.get(f"/api/evaluations/{'0' * 32}/records").status_code == 404


# ------------------------------------------------------------- 逐条对照


def test_records_are_paged_and_filterable(client: TestClient, scenario) -> None:
    outcome = scenario.evaluate()

    payload = data_of(client.get(f"/api/evaluations/{outcome.evaluation_id}/records"))
    assert payload["total"] == CALIBRATION_SIZE
    assert payload["page_size"] == 50
    # 按编号升序（人读编号是数字，不是字符串）。
    assert [int(row["record_id"]) for row in payload["items"]] == sorted(
        int(record_id) for record_id in scenario.ids
    )

    disagree = data_of(
        client.get(f"/api/evaluations/{outcome.evaluation_id}/records", params={"agree": "false"})
    )
    assert disagree["total"] == 0  # 全部判对，没有「有预测但不一致」的行

    single = data_of(
        client.get(
            f"/api/evaluations/{outcome.evaluation_id}/records",
            params={"record_id": scenario.ids[3]},
        )
    )
    assert single["total"] == 1
    assert single["items"][0]["gold_label"] in CALIBRATION_PER_CLASS
    assert single["items"][0]["agree"] is True


def test_records_separate_no_prediction_from_misprediction(
    client: TestClient, scenario
) -> None:
    """技术失败的行**没有预测**（`agree` 为空），不能被算进「不一致」。

    `agree=false` 在 SQL 里永远不匹配 NULL，所以两类不会混：这正是把「判错」与
    「没判出来」分开的口径在接口上的表现。
    """
    broken = seed_scenario(
        subdir(scenario.config.paths.database.parent, "v2"), answers=fail_nth(WRONG, 2)
    )
    outcome = broken.evaluate()
    broken_client = TestClient(create_app(broken.config))

    failed = data_of(
        broken_client.get(
            f"/api/evaluations/{outcome.evaluation_id}/records", params={"status": "failed"}
        )
    )
    assert failed["total"] == 1
    assert failed["items"][0]["agent_label"] is None
    assert failed["items"][0]["agree"] is None
    assert failed["items"][0]["failure_stage"] == "stage2"
    assert failed["items"][0]["record_status"] == "failed"

    disagree = data_of(
        broken_client.get(
            f"/api/evaluations/{outcome.evaluation_id}/records", params={"agree": "false"}
        )
    )
    assert disagree["total"] == 0  # 没判出来的那条没被混进「不一致」


def test_paging_bounds_are_enforced(client: TestClient, scenario) -> None:
    outcome = scenario.evaluate()
    url = f"/api/evaluations/{outcome.evaluation_id}/records"

    over = client.get(url, params={"page_size": 200 + 1})
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "PARAM_VALIDATION"

    assert client.get(url, params={"page_size": 200}).status_code == 200
    assert client.get(url, params={"page": 0}).status_code == 422

    history = client.get(
        f"/api/runs/{scenario.run_id}/evaluations",
        params={"page_size": EVALUATION_PAGE_SIZE_MAX + 1},
    )
    assert history.status_code == 422
    assert history.json()["error"]["code"] == "PARAM_VALIDATION"


def test_bad_filters_are_parameter_errors(client: TestClient, scenario) -> None:
    """非法筛选值 422，且带字段位置——与其它接口用同一个错误信封。"""
    response = client.get(
        f"/api/runs/{scenario.run_id}/evaluations", params={"page": "abc"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "PARAM_VALIDATION"
    assert body["request_id"]
    assert body["error"]["details"][0]["field"]
