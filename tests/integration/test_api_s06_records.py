"""S06-01 的接口集成测试：**记录列表（分页 + 筛选）与记录详情**。

对照 [plan/08 §6](../../../plan/08-API接口与数据合同.md) 逐条核对：参数、排序、`revision`、
筛选语义（多项 AND）、非法枚举与分页值的 422、列表不传 `a` 与全部 ref、详情含完整输入与
两阶段结果、`raw_output` **只在详情且只在被校验拒绝的那几次**出现。

全部走本地 SQLite、内置合成样例与模拟客户端，**零真实模型调用**。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_api_s03 import make_config, prepared_validation
from test_api_s04 import create_run
from test_api_s04_reads import auto_factory, run_worker_once, upload_sample

from aidhu_om_agent.agent.pipeline import CODE_OUTPUT_INVALID
from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.services.batches import RECORD_PAGE_SIZE_MAX


@pytest.fixture()
def app_config(tmp_path: Path):
    return make_config(tmp_path)


@pytest.fixture()
def client(app_config) -> TestClient:
    return TestClient(create_app(app_config))


def new_key() -> str:
    return uuid.uuid4().hex


def prepared_run(client: TestClient, tmp_path: Path, name: str) -> str:
    """上传样例 → 预检 → 建批次，返回 ``run_id``（不执行）。"""
    validation_id = upload_sample(client, tmp_path, name)
    return create_run(client, validation_id, new_key()).json()["data"]["run_id"]


def records_of(client: TestClient, run_id: str, **params: object) -> dict:
    response = client.get(f"/api/runs/{run_id}/records", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------- 列表：基本形状


def test_record_list_of_a_queued_batch_is_all_pending(client: TestClient, tmp_path: Path) -> None:
    """还没跑 worker：5 条全 `pending`，`label`／`review_required` 都是 null。

    这条守的是「不把没跑显示成失败」：`status` 必须如实反映未执行，而不是空列表。
    """
    run_id = prepared_run(client, tmp_path, "five-scenarios")

    data = records_of(client, run_id)

    assert data["total"] == 5
    assert data["page"] == 1 and data["page_size"] == 50
    assert data["revision"] == 0
    assert [item["status"] for item in data["items"]] == ["pending"] * 5
    assert [item["order_index"] for item in data["items"]] == [0, 1, 2, 3, 4]
    for item in data["items"]:
        assert item["label"] is None
        assert item["review_required"] is None
        assert item["reason"] is None
        assert item["failure"] is None


def test_record_list_item_carries_the_contract_fields_only(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """摘要字段与 [plan/08 §6] 逐条对应，且**不含 `a` 与任何 ref**。"""
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)

    item = records_of(client, run_id)["items"][0]

    assert set(item) == {
        "record_key",
        "record_id",
        "source_row",
        "order_index",
        "q_preview",
        "status",
        "label",
        "reason",
        "review_required",
        "failure",
    }
    assert not any(key in item for key in ("a", "refs", "ref1"))
    assert item["record_id"] and item["source_row"] >= 1
    assert item["q_preview"]


def test_record_list_after_worker_reports_labels_and_reasons(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)

    data = records_of(client, run_id)

    assert data["total"] == 5
    assert data["revision"] > 0
    assert [item["status"] for item in data["items"]] == ["completed"] * 5
    assert all(item["label"] for item in data["items"])
    assert all(item["reason"] for item in data["items"])
    assert all(isinstance(item["review_required"], bool) for item in data["items"])


# ---------------------------------------------------------------- 列表：筛选


def test_record_list_filters_by_label(client: TestClient, app_config, tmp_path: Path) -> None:
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)
    everything = records_of(client, run_id)
    target = everything["items"][0]["label"]

    data = records_of(client, run_id, label=target)

    assert data["total"] == sum(1 for i in everything["items"] if i["label"] == target)
    assert {item["label"] for item in data["items"]} == {target}


def test_record_list_filters_by_review_required(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)
    everything = records_of(client, run_id)
    expected = sum(1 for i in everything["items"] if i["review_required"])

    yes = records_of(client, run_id, review_required="true")
    no = records_of(client, run_id, review_required="false")

    assert yes["total"] == expected
    assert all(item["review_required"] is True for item in yes["items"])
    assert no["total"] == everything["total"] - expected
    assert all(item["review_required"] is False for item in no["items"])


def test_record_list_filters_by_status_and_combines_with_and(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """多项筛选是 AND：状态 × 标签的合取，而不是「任一命中」。"""
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)
    everything = records_of(client, run_id)
    label = everything["items"][0]["label"]

    only_status = records_of(client, run_id, status="completed")
    both = records_of(client, run_id, status="completed", label=label)
    impossible = records_of(client, run_id, status="pending", label=label)

    assert only_status["total"] == 5
    assert both["total"] == sum(1 for i in everything["items"] if i["label"] == label)
    # AND 的反例：`pending` 与任何标签都不共存（未分类没有标签）。
    assert impossible["total"] == 0
    assert impossible["items"] == []


def test_record_list_filters_by_record_id_exactly(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """`record_id` 是**精确匹配**，不是模糊搜索。"""
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)
    everything = records_of(client, run_id)
    exact = everything["items"][0]["record_id"]
    assert exact

    hit = records_of(client, run_id, record_id=exact)
    miss = records_of(client, run_id, record_id=exact[:-1])

    assert hit["total"] == 1
    assert hit["items"][0]["record_id"] == exact
    assert miss["total"] == 0


def test_record_list_distinguishes_input_invalid_from_failed(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """输入失败行是独立状态，不混进技术失败：`failure` 仍为 null。"""
    run_id = prepared_run(client, tmp_path, "partial-errors")
    run_worker_once(app_config)

    data = records_of(client, run_id)

    assert data["total"] >= 2
    invalid = [item for item in data["items"] if item["status"] == "input_invalid"]
    assert invalid, "partial-errors 样例应含输入失败行"
    assert all(item["failure"] is None for item in invalid)
    assert records_of(client, run_id, status="input_invalid")["total"] == len(invalid)


# ---------------------------------------------------------------- 列表：分页与参数


def test_record_list_paginates_in_order_index_order(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)

    first = records_of(client, run_id, page=1, page_size=2)
    second = records_of(client, run_id, page=2, page_size=2)
    third = records_of(client, run_id, page=3, page_size=2)

    assert [first["total"], second["total"], third["total"]] == [5, 5, 5]
    assert [i["order_index"] for i in first["items"]] == [0, 1]
    assert [i["order_index"] for i in second["items"]] == [2, 3]
    assert [i["order_index"] for i in third["items"]] == [4]
    assert second["page_size"] == 2


def test_record_list_accepts_the_page_size_ceiling(
    client: TestClient, tmp_path: Path
) -> None:
    run_id = prepared_run(client, tmp_path, "five-scenarios")

    response = client.get(
        f"/api/runs/{run_id}/records", params={"page_size": RECORD_PAGE_SIZE_MAX}
    )

    assert response.status_code == 200
    assert response.json()["data"]["page_size"] == RECORD_PAGE_SIZE_MAX


@pytest.mark.parametrize(
    "params",
    [
        {"label": "回答错误"},  # 不在三分类里
        {"status": "done"},  # 不是合法记录状态
        {"page": 0},
        {"page_size": 0},
        {"page_size": RECORD_PAGE_SIZE_MAX + 1},
    ],
)
def test_record_list_rejects_bad_params_with_the_error_envelope(
    client: TestClient, tmp_path: Path, params: dict
) -> None:
    """非法枚举与分页值一律 422，且用的是项目错误信封（不是 FastAPI 的 detail）。"""
    run_id = prepared_run(client, tmp_path, "five-scenarios")

    response = client.get(f"/api/runs/{run_id}/records", params=params)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "PARAM_VALIDATION"
    assert body["request_id"]


def test_record_list_of_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/api/runs/nope/records")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 详情


def test_record_detail_carries_full_input_and_both_stages(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)
    item = records_of(client, run_id)["items"][0]

    response = client.get(f"/api/runs/{run_id}/records/{item['record_key']}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["run_id"] == run_id
    assert data["record_key"] == item["record_key"]
    assert data["status"] == "completed"
    assert data["revision"] > 0

    # 完整输入：13 个输入列的原文，含 a 与全部 ref（列表里没有的那部分）。
    assert set(data["input"]) == {
        "编号",
        "q",
        "a",
        *(f"ref{index}" for index in range(1, 11)),
    }
    assert data["input"]["q"] and data["input"]["a"]

    # 阶段一：资料充分性 + 逐字摘录证据；阶段二：标签、理由与复核。
    assert data["stage1"]["evidence"] and data["stage1"]["evidence_sufficiency"]
    assert data["stage2"]["label"] == data["label"]
    assert data["stage2"]["reason"] == item["reason"]
    assert data["stage2"]["review_required"] is data["review_required"]
    assert data["failure"] is None
    assert data["input_error"] is None


def test_record_detail_attempt_summary_hides_successful_output(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """成功尝试**不带** `raw_output`：只有被校验拒绝的那几次才留存正文。"""
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    run_worker_once(app_config)
    record_key = records_of(client, run_id)["items"][0]["record_key"]

    data = client.get(f"/api/runs/{run_id}/records/{record_key}").json()["data"]

    assert [entry["stage"] for entry in data["attempt_summary"]] == ["stage1", "stage2"]
    for entry in data["attempt_summary"]:
        assert entry["outcome"] == "ok"
        assert entry["attempt"] == 1
        assert entry["simulated"] is True
        assert entry["model"] == "mock-deterministic"
        assert entry["raw_output"] is None
        assert entry["raw_output_truncated"] is False


def test_record_detail_exposes_rejected_output_for_failed_attempts(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """技术失败记录：被拒绝的模型正文可查看，且标记为**已截断/未截断**。

    这是 S03 返工的界面成果在**批次路径**上的承接（S06 阶段文档 §0.2 第 5 项）。
    """
    run_id = prepared_run(client, tmp_path, "forced-failure")
    run_worker_once(app_config)
    item = records_of(client, run_id)["items"][0]

    assert item["status"] == "failed"
    assert item["failure"]["code"] == CODE_OUTPUT_INVALID
    assert item["label"] is None

    data = client.get(f"/api/runs/{run_id}/records/{item['record_key']}").json()["data"]

    rejected = [e for e in data["attempt_summary"] if e["outcome"] == "validation_error"]
    assert len(rejected) == item["failure"]["attempt_count"] == 3
    assert all(e["stage"] == "stage2" for e in rejected)
    assert all(e["error_code"] == CODE_OUTPUT_INVALID for e in rejected)
    for entry in rejected:
        assert entry["raw_output"], "被拒输出必须可查看"
        assert entry["raw_output_truncated"] is False
        # 正文是模型的最终 content（这里即那段 JSON），不是推理链。
        assert "ref1" in entry["raw_output"]

    # 阶段一那次成功不带正文。
    assert data["attempt_summary"][0]["outcome"] == "ok"
    assert data["attempt_summary"][0]["raw_output"] is None


def test_rejected_output_stays_out_of_the_list(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """`raw_output` 只在详情暴露：列表里连字段都不出现。"""
    run_id = prepared_run(client, tmp_path, "forced-failure")
    run_worker_once(app_config)

    item = records_of(client, run_id)["items"][0]

    assert "attempt_summary" not in item
    assert "raw_output" not in json.dumps(item, ensure_ascii=False)


def test_record_detail_of_input_invalid_row_shows_the_reason(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """输入失败行：q/a 为 null，但原因可查（与预检报告 `row_errors` 同源）。"""
    run_id = prepared_run(client, tmp_path, "partial-errors")
    run_worker_once(app_config)
    invalid = records_of(client, run_id, status="input_invalid")["items"]
    assert invalid

    data = client.get(
        f"/api/runs/{run_id}/records/{invalid[0]['record_key']}"
    ).json()["data"]

    assert data["status"] == "input_invalid"
    assert data["input_error"]["reason"]
    assert data["failure"] is None
    assert data["stage1"] is None and data["stage2"] is None


def test_record_detail_404_for_unknown_or_foreign_record(
    client: TestClient, app_config, tmp_path: Path
) -> None:
    """别的批次的记录不能通过本批次的路径读到——不泄露它属于哪个批次。"""
    run_id = prepared_run(client, tmp_path, "five-scenarios")
    other_run_id = prepared_run(client, tmp_path, "no-refs")
    run_worker_once(app_config)
    foreign = records_of(client, other_run_id)["items"][0]["record_key"]

    unknown = client.get(f"/api/runs/{run_id}/records/nope")
    cross = client.get(f"/api/runs/{run_id}/records/{foreign}")

    assert unknown.status_code == 404
    assert cross.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"
    assert cross.json()["error"]["code"] == "NOT_FOUND"


def test_record_detail_is_readable_before_the_worker_runs(
    client: TestClient, tmp_path: Path
) -> None:
    """未执行时详情仍可打开：输入在，阶段与尝试为空——不是 404。"""
    run_id = prepared_run(client, tmp_path, "no-refs")
    record_key = records_of(client, run_id)["items"][0]["record_key"]

    data = client.get(f"/api/runs/{run_id}/records/{record_key}").json()["data"]

    assert data["status"] == "pending"
    assert data["input"]["q"]
    assert data["stage1"] is None and data["stage2"] is None
    assert data["attempt_summary"] == []
    assert data["label"] is None
