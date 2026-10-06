"""S03-06：真实服务兼容检查（默认整体跳过，须显式开启）。

真实调用会产生费用，因此本文件默认不执行；只有设置环境变量
``AIDHU_REAL_CALLS=1`` 时才运行，并受 [S03 §0.2 第 1 条](../../../docs/implementation/stages/S03-模型适配与两阶段判别.md)
的预算约束：

- 合成样例不超过 **3 条**（其中 1 条为最长样例）；
- 阶段一 + 阶段二**实际调用总数不超过 10 次**（含重试）。

预算由 `BudgetedClient` 在**调用之前**机械计数，超出立即抛错中止，
不会静默超支。证据写入 ``data/runtime/s03-06/compat-<时间戳>.json``
（``data/**`` 已被 .gitignore 忽略），报告引用其中的实际值。

**本文件只证明格式与限制兼容，不构成任何分类准确率结论**：合成样例与提示词
都未用于调参，也不作为质量评估数据。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import pytest

from aidhu_om_agent.agent.pipeline import InMemoryBudgetLedger, run_single
from aidhu_om_agent.config import AppConfig, load_config, project_root
from aidhu_om_agent.excel.reader import precheck
from aidhu_om_agent.excel.samples import build_workbook_bytes, get_sample
from aidhu_om_agent.llm.client import (
    STAGE1,
    STAGE2,
    ModelResponse,
    OpenAICompatibleClient,
)
from aidhu_om_agent.schemas.judgement import LABELS
from aidhu_om_agent.schemas.qa import ParsedInput, QARecord

pytestmark = pytest.mark.skipif(
    os.environ.get("AIDHU_REAL_CALLS") != "1",
    reason="真实调用需显式开启：设置 AIDHU_REAL_CALLS=1（会产生费用）",
)

#: §0.2 第 1 条的授权上限；超出即中止。
MAX_RECORDS = 3
MAX_CALLS = 10

#: 2026-10-06 首次执行时已经花掉的真实调用：2 次格式探针 + 2 次因测试自身缺陷
#: 中断的流程调用（该缺陷已修复，但调用费用已实际发生，仍计入总预算）。
#: 因此本次只允许再调用 ``MAX_CALLS - PRIOR_CALLS_SPENT`` 次。全新一轮执行此文件
#: 时该值应为 0。
PRIOR_CALLS_SPENT = 4

#: 真实调用用的样例与记录：(样例名, 记录编号)。含 1 条最长样例。
PLAN: tuple[tuple[str, str], ...] = (
    ("longest", "L-1"),
    ("longest", "L-2"),
    ("five-scenarios", "1"),
)

EVIDENCE_DIR = project_root() / "data" / "runtime" / "s03-06"


class BudgetedClient:
    """按真实调用次数计数的包装器；超预算立即中止，不静默继续。"""

    def __init__(self, inner: OpenAICompatibleClient, limit: int = MAX_CALLS) -> None:
        self._inner = inner
        self._limit = limit
        self.calls: list[dict[str, object]] = []

    def call(self, messages: Sequence[Mapping[str, str]], stage: str) -> ModelResponse:
        if len(self.calls) >= self._limit:
            raise AssertionError(
                f"真实调用已达授权上限 {self._limit} 次，中止以避免超支"
            )
        # 无论成功失败都先计数：失败的调用同样消耗费用。
        entry: dict[str, object] = {
            "stage": stage,
            "message_chars": sum(len(str(m.get("content", ""))) for m in messages),
            "roles": [str(m.get("role")) for m in messages],
        }
        self.calls.append(entry)
        try:
            response = self._inner.call(messages, stage)
        except Exception as exc:
            entry["outcome"] = "model_error"
            entry["error_type"] = type(exc).__name__
            entry["error_message"] = str(exc)
            raise
        entry.update(
            {
                "outcome": "ok",
                "model_returned": response.model,
                "finish_reason": response.finish_reason,
                "latency_ms": response.latency_ms,
                "content_chars": len(response.content),
                "usage": None
                if response.usage is None
                else {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
            }
        )
        return response


def _safe_snapshot(config: AppConfig) -> dict[str, object]:
    """配置快照；`snapshot()` 只暴露凭据是否存在，不含密钥值。"""
    return config.snapshot()


def _write_evidence(name: str, payload: dict[str, object]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = EVIDENCE_DIR / f"{name}-{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _record_outcome(record: QARecord, result: object) -> dict[str, object]:
    """把一条 ``RecordResult`` 摊平成证据条目。

    ``label`` 只存在于阶段二；技术失败时整条为 ``None``，不填造标签。
    """
    stage1 = result.stage1  # type: ignore[attr-defined]
    stage2 = result.stage2  # type: ignore[attr-defined]
    failure = result.failure  # type: ignore[attr-defined]
    return {
        "sample_record_id": record.record_id,
        "source_row": record.source_row,
        "q_chars": len(record.q or ""),
        "a_chars": len(record.a or ""),
        "ref_chars": [len(value or "") for value in record.refs.values()],
        "status": result.status,  # type: ignore[attr-defined]
        "has_stage1": stage1 is not None,
        "has_stage2": stage2 is not None,
        "label": stage2.label.value if stage2 is not None else None,
        "simulated": result.simulated,  # type: ignore[attr-defined]
        "failure": None
        if failure is None
        else {
            "stage": failure.stage,
            "code": failure.code,
            "attempt_count": failure.attempt_count,
            "retryable": failure.retryable,
        },
        "attempts": [
            {
                "stage": attempt.stage,
                "attempt": attempt.attempt,
                "outcome": attempt.outcome,
                "model": attempt.model,
                "latency_ms": attempt.latency_ms,
                "usage": None
                if attempt.usage is None
                else {
                    "prompt_tokens": attempt.usage.prompt_tokens,
                    "completion_tokens": attempt.usage.completion_tokens,
                    "total_tokens": attempt.usage.total_tokens,
                },
                "error_code": attempt.error_code,
            }
            for attempt in result.attempts  # type: ignore[attr-defined]
        ],
    }


@pytest.fixture(scope="module")
def real_config() -> AppConfig:
    try:
        config = load_config()
    except Exception as exc:  # 配置缺失时明确跳过，不伪装通过
        pytest.skip(f"无法读取本机配置：{type(exc).__name__} {exc}")
    if not (config.stage1.api_key and config.stage2.api_key):
        pytest.skip("本机缺少真实凭据，跳过真实兼容检查")
    return config


def _parsed_sample(tmp_path: Path, sample_name: str) -> ParsedInput:
    sample = get_sample(sample_name)
    assert sample is not None, f"样例不存在：{sample_name}"
    path = tmp_path / sample.filename
    path.write_bytes(build_workbook_bytes(sample))
    return precheck(path)


def _pick(parsed: ParsedInput, record_id: str) -> QARecord:
    for record in parsed.valid_records():
        if record.record_id == record_id:
            return record
    raise AssertionError(f"样例中没有编号为 {record_id} 的有效记录")


def test_real_service_format_probe(real_config: AppConfig) -> None:
    """两个阶段各一次小请求：记录服务实际返回的模型标识、格式与用量。

    适配器只发送 ``model`` 与 ``messages``；本用例据此记录服务是否接受该形状，
    并检查推理内容与最终内容是否同时出现（我们只解析最终内容）。
    """
    from openai import OpenAI

    results: list[dict[str, object]] = []
    for stage, model_config in (
        (STAGE1, real_config.stage1),
        (STAGE2, real_config.stage2),
    ):
        client = OpenAI(
            base_url=model_config.base_url,
            api_key=model_config.api_key,
            timeout=model_config.timeout_seconds,
            max_retries=0,
        )
        started = time.monotonic()
        completion = client.chat.completions.create(
            model=model_config.model,
            messages=[
                {"role": "system", "content": "只输出一个 JSON 对象，不要其它文字。"},
                {"role": "user", "content": '请输出 {"ok": true}'},
            ],
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = completion.choices[0]
        message = choice.message
        dumped = message.model_dump()
        results.append(
            {
                "stage": stage,
                "configured_model": model_config.model,
                "model_returned": getattr(completion, "model", None),
                "object_type": type(completion).__name__,
                "finish_reason": getattr(choice, "finish_reason", None),
                "latency_ms": latency_ms,
                "message_fields": sorted(dumped.keys()),
                "has_reasoning_content": bool(dumped.get("reasoning_content")),
                "reasoning_chars": len(str(dumped.get("reasoning_content") or "")),
                "content_chars": len(str(dumped.get("content") or "")),
                "content_preview": str(dumped.get("content") or "")[:200],
                "usage": None
                if getattr(completion, "usage", None) is None
                else completion.usage.model_dump(),
                "choice_count": len(completion.choices),
            }
        )

    path = _write_evidence("format-probe", {"calls": results})
    for item in results:
        assert item["choice_count"] == 1, "服务返回多个候选，适配器只取第一个"
        assert item["content_chars"] > 0 or item["has_reasoning_content"], (
            f"{item['stage']} 既无内容也无推理内容"
        )
    assert path.is_file()


def test_real_service_full_pipeline(
    real_config: AppConfig, tmp_path: Path
) -> None:
    """3 条合成样例（含最长样例）走完整两阶段流程，记录实际结果与用量。"""
    client = BudgetedClient(
        OpenAICompatibleClient(real_config), limit=MAX_CALLS - PRIOR_CALLS_SPENT
    )
    budget = InMemoryBudgetLedger()

    records: list[QARecord] = []
    for sample_name, record_id in PLAN:
        parsed = _parsed_sample(tmp_path, sample_name)
        record = _pick(parsed, record_id)
        records.append(record)
    assert len(records) <= MAX_RECORDS, "合成样例条数超出授权上限"

    outcomes: list[dict[str, object]] = []
    path: Path | None = None
    for record in records:
        result = run_single(
            record,
            client,
            execution=real_config.execution,
            budget=budget,
        )
        outcomes.append(_record_outcome(record, result))
        # 每完成一条就落一次证据：真实调用昂贵，中途出错也不能丢掉已得结果。
        path = _write_evidence(
            "pipeline",
            {
                "config": _safe_snapshot(real_config),
                "call_count": len(client.calls),
                "calls": client.calls,
                "records": outcomes,
                "note": "合成样例，仅证明格式与限制兼容；不构成分类质量结论。",
            },
        )

    assert path is not None and path.is_file()
    assert len(client.calls) <= MAX_CALLS, "真实调用次数超出授权上限"
    for outcome in outcomes:
        assert outcome["simulated"] is False, "真实流程的结果不能带模拟标记"
        if outcome["status"] == "completed":
            assert outcome["label"] in LABELS, (
                f"返回标签不在三分类内：{outcome['label']}"
            )
            assert outcome["failure"] is None
        else:
            assert outcome["label"] is None, "技术失败不得填造标签"
