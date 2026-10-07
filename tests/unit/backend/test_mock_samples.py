"""S03-05 的确定性模拟客户端单测。

模拟结果不能当作模型质量证据，但**必须能通过真实校验**，否则界面模拟模式连
交互都验证不了。这里逐情境核对：同一输入两次结果完全一致、证据取自真实 ref、
输出能过 `agent/validation.py`、且带 ``simulated=True``。
"""

from __future__ import annotations

import pytest

from aidhu_om_agent.agent.mock_samples import (
    ALL_SCENARIOS,
    FORCED_SCENARIOS,
    SCENARIOS,
    SCENARIO_FORCED_FAILURE,
    SCENARIO_LABELS,
    SLOW_RECORD_DELAYS_MS,
    MockClient,
    scenario_for,
)
from aidhu_om_agent.agent.pipeline import run_single
from aidhu_om_agent.agent.stage1 import build_stage1_messages, stage1_messages_contain_a
from aidhu_om_agent.agent.validation import parse_stage1, parse_stage2
from aidhu_om_agent.config import ExecutionConfig
from aidhu_om_agent.llm.client import STAGE1, STAGE2
from aidhu_om_agent.schemas.judgement import LABELS
from aidhu_om_agent.schemas.qa import REF_FIELDS, QARecord

REF1 = "校园卡补办需携带学生证，到校园卡中心办理。"
REF2 = "办理时间为工作日 9:00—17:00。"


def make_record(**overrides: object) -> QARecord:
    refs: dict[str, str | None] = {field: None for field in REF_FIELDS}
    refs["ref1"] = REF1
    refs["ref2"] = REF2
    data: dict[str, object] = {
        "record_id": "1",
        "source_row": 2,
        "order_index": 0,
        "q": "如何补办校园卡？",
        "a": "带学生证去校园卡中心。",
        "refs": refs,
    }
    data.update(overrides)
    return QARecord(**data)  # type: ignore[arg-type]


def empty_refs_record(**overrides: object) -> QARecord:
    return make_record(refs={field: None for field in REF_FIELDS}, **overrides)


# ---------------------------------------------------------------- 情境选择


def test_scenario_is_stable_for_the_same_record() -> None:
    assert scenario_for(make_record()) == scenario_for(make_record())


def test_scenario_is_one_of_the_declared_set() -> None:
    assert scenario_for(make_record()) in SCENARIOS
    # 情境说明必须覆盖全部可接受情境：五类常规 + 强制技术失败。
    assert set(SCENARIO_LABELS) == set(ALL_SCENARIOS)
    assert set(SCENARIOS) <= set(ALL_SCENARIOS)


def test_record_without_refs_forces_insufficient() -> None:
    # 没有资料就不可能「回答正确」：强制走资料不足，模拟输出才能通过校验。
    assert scenario_for(empty_refs_record()) == "insufficient"


def test_scenario_falls_back_to_row_when_id_missing() -> None:
    assert scenario_for(make_record(record_id=None)) in SCENARIOS


def test_unknown_scenario_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知模拟情境"):
        MockClient(make_record(), scenario="不存在的场景")


# ---------------------------------------------------------------- 输出一致性


def test_same_record_gives_identical_output() -> None:
    first = MockClient(make_record())
    second = MockClient(make_record())

    for stage in (STAGE1, STAGE2):
        assert first.call([], stage).content == second.call([], stage).content


def test_scenarios_cover_every_variant_across_ids() -> None:
    scenarios = {scenario_for(make_record(record_id=str(index))) for index in range(200)}

    assert scenarios == set(SCENARIOS)


def test_mock_response_is_marked_simulated() -> None:
    response = MockClient(make_record()).call([], STAGE1)

    assert response.simulated is True
    assert response.model == "mock-deterministic"
    assert response.usage is None  # 模拟不产生真实用量，不用 0 冒充
    assert response.finish_reason == "stop"


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知阶段"):
        MockClient(make_record()).call([], "stage3")


# --------------------------------------------------- S04-07：慢速样例的等待


def test_normal_record_does_not_wait() -> None:
    """除慢速样例的编号之外，模拟调用保持零等待（默认行为不变）。"""
    slept: list[float] = []
    response = MockClient(make_record(), sleep=slept.append).call([], STAGE1)

    assert slept == []
    assert response.latency_ms == 0


def test_slow_record_waits_and_reports_the_wait() -> None:
    """慢速编号真的等待，且 ``latency_ms`` 如实返回这段等待（不报 0 掩盖）。"""
    record_id = next(iter(SLOW_RECORD_DELAYS_MS))
    expected_ms = SLOW_RECORD_DELAYS_MS[record_id]
    slept: list[float] = []
    client = MockClient(make_record(record_id=record_id), sleep=slept.append)

    response = client.call([], STAGE1)

    assert slept == [expected_ms / 1000]
    assert response.latency_ms == expected_ms
    # 等待只影响耗时，不影响结果：同一情境仍是确定性输出。
    assert response.simulated is True


# ---------------------------------------------------------------- 通过真实校验


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_passes_real_validation(scenario: str) -> None:
    record = make_record()
    client = MockClient(record, scenario=scenario)

    parse_stage1(client.call([], STAGE1).content, record)
    parse_stage2(client.call([], STAGE2).content, record)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_labels_are_business_labels(scenario: str) -> None:
    record = make_record()
    judgement = parse_stage2(MockClient(record, scenario=scenario).call([], STAGE2).content, record)

    assert judgement.label.value in LABELS


def test_no_refs_record_parses_in_every_scenario() -> None:
    record = empty_refs_record()

    parse_stage1(MockClient(record).call([], STAGE1).content, record)
    parse_stage2(MockClient(record).call([], STAGE2).content, record)


def test_evidence_quote_is_a_real_ref_substring() -> None:
    record = make_record()
    analysis = parse_stage1(MockClient(record, scenario="correct").call([], STAGE1).content, record)

    assert analysis.evidence
    for item in analysis.evidence:
        assert item.quote in record.refs[item.ref_id]  # type: ignore[operator]


# ---------------------------------------------------------------- 请求记录


def test_calls_are_recorded_for_boundary_checks() -> None:
    record = make_record()
    client = MockClient(record)
    client.call(build_stage1_messages(record), STAGE1)

    assert stage1_messages_contain_a(client.messages_for(STAGE1), record) is False


def test_messages_for_unknown_stage_raises() -> None:
    with pytest.raises(KeyError):
        MockClient(make_record()).messages_for(STAGE2)


# ---------------------------------------------------------------- 端到端（仍为模拟）


def test_run_single_with_mock_client_completes_and_is_flagged() -> None:
    record = make_record()
    result = run_single(
        record,
        MockClient(record),
        execution=ExecutionConfig(
            concurrency=1, max_attempts_per_stage_campaign=3, retry_backoff_seconds=(2, 4)
        ),
        sleep=lambda _: None,
    )

    assert result.status == "completed"
    assert result.simulated is True
    assert result.stage2 is not None
    assert result.stage2.label.value in LABELS
    assert [attempt.stage for attempt in result.attempts] == [STAGE1, STAGE2]
