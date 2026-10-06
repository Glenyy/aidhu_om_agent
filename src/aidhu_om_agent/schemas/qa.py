"""S02-01、S02-02、S02-04：13 列输入合同、文本归一化与记录/预检类型。

本模块只描述**输入侧**的数据合同：列名、编号与文本归一化规则、单条记录、
证据以及预检报告。业务三分类枚举见 `schemas/judgement.py`，模型输出的解析
与重试属于 S03。

规则依据：plan/01 §2（输入合同）、plan/08 §3（预检结果字段）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 输入解析与规范版本：每次修改列名、编号或归一化规则都必须递增。
INPUT_CONTRACT_VERSION = "1.0"

ID_COLUMN = "编号"
Q_COLUMN = "q"
A_COLUMN = "a"
REF_FIELDS: tuple[str, ...] = tuple(f"ref{i}" for i in range(1, 11))
REF_COUNT = len(REF_FIELDS)

#: 正式输入工作簿必须有的 13 列，顺序即文档约定顺序。
INPUT_COLUMNS: tuple[str, ...] = (ID_COLUMN, Q_COLUMN, A_COLUMN, *REF_FIELDS)

#: 预检状态：passed 表示可创建批次，blocked 表示批次级问题需先修正。
PrecheckStatus = Literal["passed", "blocked"]

#: Excel 错误值；这类单元格不构成资料文本。
_ERROR_VALUES = frozenset(
    {"#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!", "#GETTING_DATA"}
)


# --------------------------------------------------------------------------
# 单元格与文本归一化（S02-01）
# --------------------------------------------------------------------------


def normalize_text(value: str) -> str:
    """统一换行为 ``\\n`` 并去掉首尾空白；中间空白保持原样。"""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def is_numeric_cell(value: object) -> bool:
    """是否为 Excel 数值型单元格（``bool`` 不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_error_cell(value: object) -> bool:
    """是否为 Excel 错误值单元格（如 ``#N/A``）。"""
    return isinstance(value, str) and value.strip() in _ERROR_VALUES


def is_formula_cell(value: object) -> bool:
    """是否为公式单元格（需以 ``data_only=False`` 读取才能看到）。"""
    return isinstance(value, str) and value.startswith("=")


def cell_to_text(value: object) -> str | None:
    """单元格值 → 归一化文本；空或仅空白返回 ``None``。

    编号归一化按 S02 阶段文档 §0：数值按十进制写出，整数不带 ``.0``
    （``7.0`` → ``"7"``）；文本原样保留，因此 ``"007"`` **不会**被改写成 ``"7"``。
    布尔按 Excel 习惯写作 ``TRUE`` / ``FALSE``。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return normalize_text(str(value)) or None


def missing_required_fields(record: QARecord) -> tuple[str, ...]:
    """有效记录要求的必填列中缺失的列名；ref 为空合法，不在此列。"""
    missing: list[str] = []
    if record.record_id is None:
        missing.append(ID_COLUMN)
    if record.q is None:
        missing.append(Q_COLUMN)
    if record.a is None:
        missing.append(A_COLUMN)
    return tuple(missing)


# --------------------------------------------------------------------------
# 记录与证据（S02-04）
# --------------------------------------------------------------------------


class QARecord(BaseModel):
    """一条非空输入行。

    记录本身不携带状态：有效还是输入失败由所属预检报告的 ``row_errors``
    按 ``source_row`` 判定（见 `ParsedInput.valid_records`）。
    """

    model_config = ConfigDict(frozen=True)

    record_id: str | None = None
    source_row: int = Field(ge=1)
    order_index: int = Field(ge=0)
    q: str | None = None
    a: str | None = None
    refs: dict[str, str | None]

    @field_validator("refs")
    @classmethod
    def _check_ref_keys(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        if tuple(value) != REF_FIELDS:
            raise ValueError(
                f"refs 必须按 {REF_FIELDS[0]}—{REF_FIELDS[-1]} 的固定顺序给出全部 "
                f"{REF_COUNT} 项，实际为 {tuple(value)}"
            )
        return value

    def ref_text(self, ref_id: str) -> str | None:
        """取某份资料原文；未知 ref 编号抛 ``KeyError``。"""
        return self.refs[ref_id]


class Evidence(BaseModel):
    """一条证据：ref 编号加对应原文摘录。"""

    model_config = ConfigDict(frozen=True)

    ref_id: str
    quote: str


# --------------------------------------------------------------------------
# 预检结果（S02-02）
# --------------------------------------------------------------------------


class Blocker(BaseModel):
    """批次级问题：存在时 status 为 blocked，不产出可处理记录。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    field: str | None = None
    source_rows: tuple[int, ...] = ()


class RowError(BaseModel):
    """单条输入问题：保留来源行与可读编号，不阻止其余有效记录。"""

    model_config = ConfigDict(frozen=True)

    source_row: int = Field(ge=1)
    record_id: str | None = None
    reason: str


class PrecheckCounts(BaseModel):
    """预检计数；未完整扫描数据行时四项均为 ``None``。

    可统计时满足 ``total = valid + input_invalid``，空行另计不进入 total。
    """

    model_config = ConfigDict(frozen=True)

    total: int | None = None
    valid: int | None = None
    input_invalid: int | None = None
    skipped_blank_rows: int | None = None


class PrecheckWarning(BaseModel):
    """不阻止创建的提示。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class PrecheckReport(BaseModel):
    """一次预检的不可变结果，字段对应 plan/08 §3。"""

    model_config = ConfigDict(frozen=True)

    input_contract_version: str = INPUT_CONTRACT_VERSION
    status: PrecheckStatus
    sheet_name: str | None = None
    file_sha256: str
    file_size_bytes: int
    counts: PrecheckCounts = PrecheckCounts()
    blockers: tuple[Blocker, ...] = ()
    row_errors: tuple[RowError, ...] = ()
    warnings: tuple[PrecheckWarning, ...] = ()


class ParsedInput(BaseModel):
    """解析快照：预检报告 + 按工作簿顺序排列的非空记录。

    快照可序列化（``model_dump``），S04 落库与 S06 预检接口直接复用。
    """

    model_config = ConfigDict(frozen=True)

    report: PrecheckReport
    records: tuple[QARecord, ...] = ()

    @property
    def invalid_source_rows(self) -> frozenset[int]:
        return frozenset(error.source_row for error in self.report.row_errors)

    def valid_records(self) -> list[QARecord]:
        """可进入后续阶段的记录。"""
        invalid = self.invalid_source_rows
        return [record for record in self.records if record.source_row not in invalid]

    def invalid_records(self) -> list[QARecord]:
        """输入失败记录；其 q/a 保持缺失，不填造内容。"""
        invalid = self.invalid_source_rows
        return [record for record in self.records if record.source_row in invalid]
