"""S03-05：确定性合成响应客户端（模拟模式）。

用户 2026-10-06 决定：界面模拟模式采用**确定性合成响应**，不经网络、不消耗任何
真实调用，用于验证交互、流程与边界。

设计要点：

- 同一记录 + 同一情境 → 完全相同的输出（``zlib.crc32`` 选情境，不用随机数）。
- 证据摘录取自该记录**真实的** ref 原文子串，因此合成响应能通过
  `agent/validation.py` 的引用位置校验——否则模拟结果连结构都不合法，
  就失去了验证交互的意义。
- 所有响应都带 ``simulated=True``，调用方必须据此显著标识，防止被当成真实结果。
- `MockClient.calls` 保留收到的消息，供自动化审阅核对请求边界（阶段一不含 a）。
"""

from __future__ import annotations

import json
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..llm.client import STAGE1, STAGE2, ModelResponse
from ..schemas.qa import QARecord

#: 五种被覆盖的情境；与 S03 阶段文档 §2.4／§4 的手动指南一一对应。
SCENARIOS: tuple[str, ...] = (
    "correct",
    "insufficient",
    "wrong",
    "correction",
    "forced_review",
)

#: **技术失败**情境（2026-10-06 返工新增）：刻意返回引用了不存在原文的伪造摘录，
#: 使它走真实的校验路径并被拒。它**不属于** `SCENARIOS`，因此
#: ``SCENARIOS[crc32 % 5]`` 的映射与既有五类覆盖完全不变；只能由记录编号显式命中。
SCENARIO_FORCED_FAILURE = "forced_failure"

#: 显式编号 → 情境的强制映射。用于让失败界面在**模拟模式**下零成本可验。
FORCED_SCENARIOS: dict[str, str] = {"FF-1": SCENARIO_FORCED_FAILURE}

#: 显式编号 → 每次调用的固定等待（毫秒）。S04-07 用它把整批耗时拉长到数秒，让用户
#: **真的能在运行中停掉 worker** 制造中断——现有 `MockClient` 是零延迟，没有这个
#: 窗口就只能对着已经跑完的批次谈恢复（S04 阶段文档 §0.4「慢速样例是必要条件」）。
#: **不新增配置项**：等待由记录编号决定，只在模拟模式生效。
SLOW_RECORD_DELAYS_MS: dict[str, int] = {
    "SLOW-1": 1000,
    "SLOW-2": 1000,
    "SLOW-3": 1000,
}

#: 模拟模式接受的全部情境（五类常规 + 一类强制技术失败）。
ALL_SCENARIOS: tuple[str, ...] = (*SCENARIOS, SCENARIO_FORCED_FAILURE)

#: 情境说明，供界面与手动指南展示。
SCENARIO_LABELS: dict[str, str] = {
    "correct": "回答正确",
    "insufficient": "资料不足",
    "wrong": "答案错误",
    "correction": "重大更正",
    "forced_review": "强制复核",
    SCENARIO_FORCED_FAILURE: "技术失败（输出被拒）",
}

#: 强制失败情境伪造的摘录：故意**不是**任何 ref 的原文子串，且带明显标记，
#: 防止它被误当成真实原文。
_FABRICATED_QUOTE = "（模拟技术失败）这段摘录在原资料里并不存在，用于验证失败界面。"

_QUOTE_LIMIT = 40
_EMPTY_REF_NOTE = "ref1—ref10 均为空，没有任何资料可用于核对核心作答要点"
#: 「资料不足」情境落在**有 ref** 的记录上时的缺口语（由 crc32 也可能选中该情境）。
#: 此时不能说「均为空」——那与记录实际内容不符，会误导界面复核者。
_PARTIAL_REF_NOTE = "（模拟）现有资料只覆盖部分要点，核心作答要点无法据以确认"


def _first_ref(record: QARecord) -> tuple[str, str] | None:
    """第一个非空 ref 及其可作摘录的原文子串；没有则返回 ``None``。"""
    for ref_id, text in record.refs.items():
        if text and text.strip():
            return ref_id, text[:_QUOTE_LIMIT]
    return None


def scenario_for(record: QARecord) -> str:
    """按编号稳定选情境；编号缺失时按来源行。

    没有任何非空 ref 时强制为 ``insufficient``——否则合成的“回答正确”会缺少
    证据，与阶段一校验规则冲突。该规则优先于强制映射，避免在空资料记录上
    合成一份连阶段一都过不了的输出。
    """
    if _first_ref(record) is None:
        return "insufficient"
    forced = FORCED_SCENARIOS.get(record.record_id or "")
    if forced is not None:
        return forced
    seed = record.record_id or f"row:{record.source_row}"
    return SCENARIOS[zlib.crc32(seed.encode("utf-8")) % len(SCENARIOS)]


def _evidence(record: QARecord) -> list[dict[str, str]]:
    first = _first_ref(record)
    if first is None:
        return []
    return [{"ref_id": first[0], "quote": first[1]}]


def _stage1_payload(record: QARecord, scenario: str) -> dict[str, Any]:
    evidence = _evidence(record)
    quoted = evidence[0]["ref_id"] if evidence else None

    if scenario == "insufficient":
        return {
            "required_points": ["办理地点与所需材料"],
            "evidence_sufficiency": "insufficient",
            "evidence": [],
            "missing_information": [
                _EMPTY_REF_NOTE if _first_ref(record) is None else _PARTIAL_REF_NOTE
            ],
            "conflicts": [],
            "reason": "（模拟）现有资料不足以支持核心作答要点。",
        }
    if scenario == "forced_review":
        return {
            "required_points": ["办理地点与所需材料", "办理时间要求"],
            "evidence_sufficiency": "uncertain",
            "evidence": evidence,
            "missing_information": ["缺少提问时间，无法确定适用哪一版说明"],
            "conflicts": [],
            "reason": "（模拟）资料存在但适用性无法确定。",
        }
    return {
        "required_points": ["办理地点与所需材料"],
        "evidence_sufficiency": "sufficient",
        "evidence": evidence,
        "missing_information": [],
        "conflicts": [],
        "reason": f"（模拟）{quoted} 覆盖了核心作答要点。",
    }


def _stage2_payload(record: QARecord, scenario: str) -> dict[str, Any]:
    evidence = _evidence(record)
    quoted = evidence[0]["ref_id"] if evidence else None
    sufficiency = "insufficient" if scenario == "insufficient" else (
        "uncertain" if scenario == "forced_review" else "sufficient"
    )

    if scenario == SCENARIO_FORCED_FAILURE:
        # 结构合法、证据伪造：正好走到逐字子串校验并被拒，
        # 从而在模拟模式里复现真实的 OUTPUT_INVALID 失败界面。
        return {
            "label": "回答正确",
            "reason": "（模拟）刻意给出不存在的摘录，用于验证技术失败界面。",
            "issues": [],
            "evidence_sufficiency": sufficiency,
            "evidence": [{"ref_id": (quoted or "ref1"), "quote": _FABRICATED_QUOTE}],
            "review_required": False,
            "review_reasons": [],
            "stage1_corrections": [],
        }
    if scenario == "correct":
        return {
            "label": "回答正确",
            "reason": f"（模拟）{quoted} 支持回答的核心结论。",
            "issues": [],
            "evidence_sufficiency": sufficiency,
            "evidence": evidence,
            "review_required": False,
            "review_reasons": [],
            "stage1_corrections": [],
        }
    if scenario == "wrong":
        return {
            "label": "检索到正确资料但回答错误",
            "reason": f"（模拟）{quoted} 提供了正确依据，但回答遗漏了必要条件。",
            "issues": ["遗漏了所需材料这一必要条件"],
            "evidence_sufficiency": sufficiency,
            "evidence": evidence,
            "review_required": True,
            "review_reasons": ["存在判断不确定性"],
            "stage1_corrections": [],
        }
    if scenario == "correction":
        return {
            "label": "未检索到正确资料",
            "reason": "（模拟）核对原文后更正阶段一结论：资料只覆盖部分要点。",
            "issues": ["回答误用了只覆盖部分需求的资料"],
            "evidence_sufficiency": "insufficient",
            "evidence": evidence,
            "review_required": True,
            "review_reasons": ["对阶段一作出影响最终分类的重大更正"],
            "stage1_corrections": [
                {
                    "corrected_item": "证据充分性：sufficient → insufficient",
                    "reason": f"（模拟）{quoted} 只覆盖部分核心作答要点。",
                    "evidence": evidence,
                    "affects_classification": True,
                }
            ],
        }
    if scenario == "forced_review":
        return {
            "label": "未检索到正确资料",
            "reason": "（模拟）缺少提问时间，无法确定适用资料。",
            "issues": ["无法确定适用版本"],
            "evidence_sufficiency": "uncertain",
            "evidence": evidence,
            "review_required": True,
            "review_reasons": ["必要提问时间缺失，适用性无法确定"],
            "stage1_corrections": [],
        }
    # insufficient
    return {
        "label": "未检索到正确资料",
        "reason": "（模拟）现有资料不足以支持核心需求的正确回答。",
        "issues": [],
        "evidence_sufficiency": "insufficient",
        "evidence": [],
        "review_required": False,
        "review_reasons": [],
        "stage1_corrections": [],
    }


class MockClient:
    """确定性模拟客户端；实现 `llm.client.ModelClient` 协议。

    每次 ``call`` 都记录收到的消息（``self.calls``），使自动化审阅能核对
    阶段一请求不含 ``a``、阶段二含全部原始 ref。

    ``delay_ms`` 由记录编号决定（`SLOW_RECORD_DELAYS_MS`）：默认 0，只有慢速样例
    的记录会真的等待，用于制造可被中断的整批窗口。``latency_ms`` 如实返回这段等待，
    因此尝试表里的耗时不与真实经历的时间矛盾。
    """

    def __init__(
        self,
        record: QARecord,
        scenario: str | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.record = record
        self.scenario = scenario or scenario_for(record)
        if self.scenario not in ALL_SCENARIOS:
            raise ValueError(
                f"未知模拟情境 {self.scenario!r}；只接受 {'、'.join(ALL_SCENARIOS)}"
            )
        self.delay_ms = int(SLOW_RECORD_DELAYS_MS.get(record.record_id or "", 0))
        self._sleep = sleep
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def call(
        self, messages: Sequence[Mapping[str, str]], stage: str
    ) -> ModelResponse:
        self.calls.append((stage, [dict(message) for message in messages]))
        if stage == STAGE1:
            payload = _stage1_payload(self.record, self.scenario)
        elif stage == STAGE2:
            payload = _stage2_payload(self.record, self.scenario)
        else:
            raise ValueError(f"未知阶段 {stage!r}")

        if self.delay_ms:
            self._sleep(self.delay_ms / 1000)

        return ModelResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model="mock-deterministic",
            usage=None,  # 模拟不产生真实用量，不用 0 冒充
            latency_ms=self.delay_ms,
            finish_reason="stop",
            simulated=True,
        )

    def messages_for(self, stage: str) -> list[dict[str, str]]:
        """最后一次发往某阶段的消息；未调用过时抛 ``KeyError``。"""
        for called_stage, messages in reversed(self.calls):
            if called_stage == stage:
                return messages
        raise KeyError(f"尚未调用过 {stage}")


__all__ = [
    "ALL_SCENARIOS",
    "FORCED_SCENARIOS",
    "SCENARIOS",
    "SCENARIO_FORCED_FAILURE",
    "SCENARIO_LABELS",
    "SLOW_RECORD_DELAYS_MS",
    "MockClient",
    "scenario_for",
]
