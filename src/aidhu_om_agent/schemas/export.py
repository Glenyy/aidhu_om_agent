"""S05-01：导出字段与编码合同。

本模块**只描述导出产物的形状**：四张工作表的列序、JSONL 每行的字段与顺序、
人读序号、长文本截断、公式注入防护、下载文件名规则。它不读数据库，也不生成
文件——快照装载与文件写出在 `services/exports.py` 与 `excel/writer.py`。

规则依据：plan/05 §6（四表阅读方式）、plan/08 §7（导出与下载）、plan/09 §5
（exports/artifacts）、S05 阶段文档 §0.1 九项用户决定与 §0.2 第 15 项。

三条贯穿全文件的口径：

- **不填造**。未处理与失败行留空，不写"未知""暂无"之类的占位文字；空字符串
  与 ``None`` 在导出里都是留空。
- **人读序号 1..N 由导出层生成**。`order_index` 在存储层保持 0 起
  （[plan/09 §3]），这里只做展示用的 ``order_index + 1``，不改存储值。
- **JSONL 是完整原文的唯一出处**。Excel 受单元格上限约束要截断，JSONL 不截断。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .qa import REF_FIELDS

#: 导出合同版本：列序、字段名、截断与防护规则变化时递增。
EXPORT_CONTRACT_VERSION = "1.0"

# --------------------------------------------------------------------------
# 四张工作表
# --------------------------------------------------------------------------

SHEET_CLASSIFICATION = "分类结果"
SHEET_REVIEW = "复核清单"
SHEET_FAILURES = "失败清单"
SHEET_SUMMARY = "运行概况"

#: 工作表顺序即 Excel 里标签页的顺序。
SHEET_ORDER: tuple[str, ...] = (
    SHEET_CLASSIFICATION,
    SHEET_REVIEW,
    SHEET_FAILURES,
    SHEET_SUMMARY,
)

ORDINAL_COLUMN = "序号"

#: 分类结果：业务阅读主表。失败/未处理行的「原预测」「理由」「证据」留空。
CLASSIFICATION_COLUMNS: tuple[str, ...] = (
    ORDINAL_COLUMN,
    "原始编号",
    "来源行",
    "原预测",
    "理由",
    "证据",
    "需复核",
    "处理状态",
)

#: 复核清单：被标记记录的完整输入 + **两个独立的人工填写列**（导出时留空）。
#: 人工复核文件**不回读、不覆盖原预测**（plan/05 §7）。
REVIEW_HUMAN_COLUMNS: tuple[str, ...] = ("人工判断", "复核说明")

REVIEW_COLUMNS: tuple[str, ...] = (
    ORDINAL_COLUMN,
    "原始编号",
    "来源行",
    "q",
    "a",
    *REF_FIELDS,
    "原预测",
    "理由",
    "证据",
    "需复核",
    *REVIEW_HUMAN_COLUMNS,
)

#: 失败清单：只列有效记录的技术失败（输入失败是另一种状态，见「分类结果」）。
FAILURE_COLUMNS: tuple[str, ...] = (
    ORDINAL_COLUMN,
    "原始编号",
    "来源行",
    "失败阶段",
    "错误码",
    "错误信息",
    "尝试次数",
    "是否可重试",
)

#: 运行概况是**两列**的键值表，不是宽表。
SUMMARY_COLUMNS: tuple[str, ...] = ("项目", "值")

#: 概况表必须出现的行（顺序即写入顺序）；S05-02 按此装配，测试按此核对。
#: 计数一行一项而不是塞成一串 JSON：概况表是给人核对的，「未处理」单独成行才能
#: 一眼看出这份导出是不是完整批次。
SUMMARY_FIELDS: tuple[str, ...] = (
    "批次标识",
    "来源文件",
    "工作表",
    "输入摘要",
    "批次状态（捕获时）",
    "捕获时间",
    "捕获修订",
    "总数",
    "有效记录",
    "输入失败",
    "已分类",
    "技术失败",
    "未处理",
    "需复核",
    "被截断单元格数",
    "被截断单元格位置",
    "文本前缀防护单元格数",
    "清理控制字符单元格数",
    "清理控制字符位置",
    "程序版本",
    "提示词版本",
    "阶段一 schema 版本",
    "阶段二 schema 版本",
    "输入合同版本",
    "导出合同版本",
    "JSONL 文件",
    "JSONL 文件摘要",
    "Excel 文件摘要",
)

#: 概况表里「Excel 文件摘要」的固定说明：文件摘要无法写进它自己的文件里。
EXCEL_DIGEST_NOTE = "见批次详情导出区块（文件无法包含自身摘要）"

#: 概况表里代表「这份导出还不完整」的那一行。界面与概况表都必须让用户看见它，
#: 不能因为批次 running/queued 就把部分结果当成完整成功批次（plan/08 §7）。
UNPROCESSED_FIELD = "未处理"


# --------------------------------------------------------------------------
# Excel 单元格能力与两种文本改写
# --------------------------------------------------------------------------

#: Excel 单元格文本上限（含所有字符）。超出即触发截断，**不静默丢数据**。
EXCEL_CELL_MAX_CHARS = 32767

#: 截断标记；写进被截断的单元格末尾，同时概况表记数与位置。
TRUNCATION_MARKER = "…【已截断，完整原文见 JSONL】"

#: 会触发公式求值的首字符；Excel 也把 `+`、`-`、`@` 当公式开头。
FORMULA_TRIGGER_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")

#: 文本标记前缀。用户 2026-10-07 决定用**前缀**强制当文本（不是改单元格类型），
#: 单引号是 Excel 自己的「这是文本」标记。JSONL 保留未加前缀的原文。
TEXT_GUARD_PREFIX = "'"

#: openpyxl 拒绝写入的控制字符（`\x00`—`\x08`、`\x0b`—`\x0c`、`\x0e`—`\x1f`）；
#: 制表符 `\x09` 与换行 `\x0a` 是合法单元格内容，**不清理**。
#: 依赖的 `openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE` 是私有名，不引用，
#: 这里自带一份并在单测里与 openpyxl 的实际行为对齐。
ILLEGAL_CELL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
ILLEGAL_CELL_CHARS: str = "\x00-\x08\x0b\x0c\x0e-\x1f"


def strip_illegal_cell_chars(text: str) -> tuple[str, int]:
    """删掉 Excel 不接受的控制字符，返回 ``(清理后文本, 删掉的字符数)``。

    删而不是替换：这些字符在文件里不可见，替换成任何字符都只是把噪声换个样子。
    原文在 JSONL 里完整保留，清理同样在概况表记数与位置，不静默。
    """
    if not ILLEGAL_CELL_CHARS_RE.search(text):
        return text, 0
    cleaned = ILLEGAL_CELL_CHARS_RE.sub("", text)
    return cleaned, len(text) - len(cleaned)


@dataclass(frozen=True)
class ExcelCell:
    """一个待写入单元格的最终文本，以及它为了落到 Excel 里被改写过什么。

    ``text`` 是**实际写入**的字符串；其余三个是事实标记，供概况表统计与单测
    断言。原始文本不在这里——它由调用方从快照直接取，写进 JSONL。
    """

    text: str
    truncated: bool = False
    guarded: bool = False
    sanitized: int = 0

    @property
    def rewritten(self) -> bool:
        return self.truncated or self.guarded or bool(self.sanitized)


def is_truncatable(text: str) -> bool:
    """该文本写入 Excel 是否必须截断。"""
    return len(text) > EXCEL_CELL_MAX_CHARS


def truncate_for_excel(text: str) -> tuple[str, bool]:
    """按单元格上限截断，返回 ``(写入文本, 是否被截断)``。

    标记算在 32767 里：不被截断时原样返回；被截断时保留头部长
    ``EXCEL_CELL_MAX_CHARS - len(标记)`` 个字符再加标记。尾部的字符不在
    Excel 里，但在 JSONL 里，所以截断不丢数据。
    """
    if not is_truncatable(text):
        return text, False
    keep = max(EXCEL_CELL_MAX_CHARS - len(TRUNCATION_MARKER), 0)
    return text[:keep] + TRUNCATION_MARKER, True


def needs_text_guard(text: str) -> bool:
    """文本是否会被 Excel 当公式（以 ``=``、``+``、``-``、``@`` 开头）。"""
    return text.startswith(FORMULA_TRIGGER_PREFIXES)


def excel_cell_text(value: str | None) -> ExcelCell:
    """把快照里的文本编码成可写入 Excel 的单元格。

    三步的**顺序是有约束的**：

    1. 先删 Excel 不接受的控制字符——它可能就排在开头，会顶掉防护判断；
    2. 再按清理后的首字符决定是否加文本前缀——加在前缀之前判断，一个以
       ``\\x0b=1+1`` 开头的资料才不会被漏掉；
    3. 最后截断。前缀与截断抢同一分长度预算：32999 字符且以 ``=`` 开头的资料，
       若先截断到 32767 再加前缀就成了 32768 字符、写入再次越界；先加前缀再
       截断，前缀留在头部、标记顶掉尾部，长度回到上限以内。

    空值与空字符串留空（不写占位文字）。
    """
    if not value:
        return ExcelCell("")
    text, sanitized = strip_illegal_cell_chars(value)
    if not text:
        return ExcelCell("", sanitized=sanitized)
    guarded = needs_text_guard(text)
    if guarded:
        text = TEXT_GUARD_PREFIX + text
    text, truncated = truncate_for_excel(text)
    return ExcelCell(
        text, truncated=truncated, guarded=guarded, sanitized=sanitized
    )


def cell_location(sheet: str, column: str, row: int, original_length: int) -> str:
    """概况表里的「被截断单元格位置」条目，例如 ``分类结果!理由 第 3 行（原 41234 字符）``。

    ``row`` 是**该工作表里的物理行号**（表头在第 1 行），不是「序号」列的值：
    概况表没有序号列，四张表统一用行号，人在 Excel 里按行号能直接定位。
    """
    return f"{sheet}!{column} 第 {row} 行（原 {original_length} 字符）"


def sanitized_cell_location(
    sheet: str, column: str, row: int, removed: int
) -> str:
    """概况表里的「清理控制字符位置」条目，例如 ``复核清单!ref3 第 7 行（清理 2 个控制字符）``。

    行号口径同 `cell_location`。
    """
    return f"{sheet}!{column} 第 {row} 行（清理 {removed} 个控制字符）"


# --------------------------------------------------------------------------
# 人读序号与展示映射
# --------------------------------------------------------------------------


def ordinal(order_index: int) -> int:
    """展示用序号 1..N；**不改存储层的 0 起 order_index**（plan/09 §3）。"""
    return int(order_index) + 1


#: 记录状态 -> 中文；键与 plan/10 §1 的记录状态同值。
RECORD_STATUS_TEXT: dict[str, str] = {
    "pending": "未处理",
    "stage1_done": "阶段一完成",
    "completed": "已分类",
    "failed": "技术失败",
    "input_invalid": "输入失败",
}

#: 批次状态 -> 中文；键与 `runs` 表的 CHECK 同值。
RUN_STATUS_TEXT: dict[str, str] = {
    "queued": "排队中",
    "running": "执行中",
    "completed": "已完成",
    "partial_failed": "部分失败",
    "failed": "失败",
    "interrupted": "已中断",
}

#: 失败阶段 -> 中文；`failure_stage` 存的是 "stage1"/"stage2"。
STAGE_TEXT: dict[str, str] = {"stage1": "阶段一", "stage2": "阶段二"}


def record_status_text(status: str) -> str:
    """记录状态的中文展示；未知值原样返回（不猜、不吞）。"""
    return RECORD_STATUS_TEXT.get(status, status)


def run_status_text(status: str) -> str:
    """批次状态写成 ``中文（原值）``，让概况表既好读又可与库/界面逐字核对。"""
    return f"{RUN_STATUS_TEXT.get(status, status)}（{status}）"


def stage_text(stage: str | None) -> str:
    """失败阶段的中文展示；``None`` 留空。"""
    if not stage:
        return ""
    return STAGE_TEXT.get(stage, stage)


def review_required_text(value: object) -> str:
    """「需复核」列：``1`` -> 是、``0`` -> 否、缺失 -> 留空。"""
    if value is None:
        return ""
    return "是" if int(value) else "否"


def retryable_text(value: object) -> str:
    """「是否可重试」列：``True`` -> 是、``False`` -> 否、缺失 -> 留空。"""
    if value is None:
        return ""
    return "是" if value else "否"


def format_evidence(evidence: Iterable[Mapping[str, object]] | None) -> str:
    """证据列表 -> ``ref3：原文摘录`` 逐行拼接；没有证据时留空。

    引用原文按文本原样写出，**不执行其中的任何指令**（plan/08 §1）。
    """
    if not evidence:
        return ""
    lines: list[str] = []
    for item in evidence:
        ref_id = str(item.get("ref_id", ""))
        quote = str(item.get("quote", ""))
        lines.append(f"{ref_id}：{quote}" if ref_id else quote)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 下载名与媒体类型
# --------------------------------------------------------------------------

EXCEL_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
JSONL_MEDIA_TYPE = "application/x-ndjson"

#: 产物种类；与 `artifacts.kind` 的 CHECK 同值。
ARTIFACT_EXCEL = "excel"
ARTIFACT_JSONL = "jsonl"
ARTIFACT_KINDS: tuple[str, ...] = (ARTIFACT_EXCEL, ARTIFACT_JSONL)

ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    ARTIFACT_EXCEL: EXCEL_MEDIA_TYPE,
    ARTIFACT_JSONL: JSONL_MEDIA_TYPE,
}

#: 中文可读名 + 短 id（用户 2026-10-07 决定）：分类结果-<批次短id>-<导出短id>.xlsx
EXCEL_DOWNLOAD_STEM = "分类结果"
JSONL_DOWNLOAD_STEM = "两阶段明细"
ARTIFACT_SUFFIXES: dict[str, str] = {ARTIFACT_EXCEL: ".xlsx", ARTIFACT_JSONL: ".jsonl"}

#: 短 id 取前 8 个字符；run_id/export_id 都是程序生成的 hex，不含路径字符。
SHORT_ID_LENGTH = 8

#: 导出来源；与 `exports.source` 的 CHECK 同值。
SOURCE_AUTOMATIC = "automatic"
SOURCE_MANUAL = "manual"
EXPORT_SOURCES: tuple[str, ...] = (SOURCE_AUTOMATIC, SOURCE_MANUAL)


def short_id(identifier: str, length: int = SHORT_ID_LENGTH) -> str:
    """取标识的短前缀；短于该长度时原样返回。"""
    return identifier[: max(int(length), 1)]


def download_name(kind: str, *, run_id: str, export_id: str) -> str:
    """下载文件名。只有程序生成的 id 参与拼接，**用户文件名不进下载名**。"""
    if kind not in ARTIFACT_SUFFIXES:
        raise ValueError(f"未知产物种类 {kind!r}；只接受 {'、'.join(ARTIFACT_KINDS)}")
    stem = EXCEL_DOWNLOAD_STEM if kind == ARTIFACT_EXCEL else JSONL_DOWNLOAD_STEM
    return (
        f"{stem}-{short_id(run_id)}-{short_id(export_id)}{ARTIFACT_SUFFIXES[kind]}"
    )


# --------------------------------------------------------------------------
# JSONL 行合同
# --------------------------------------------------------------------------

#: 文件名前缀与「运行概况」里的叫法不同是有意的：概况表说的是**这次导出**的
#: 两份文件，JSONL 说的是**两阶段明细**这一份内容。
JSONL_RECORD_FIELDS: tuple[str, ...] = (
    "export_id",
    "run_id",
    "ordinal",
    "record_key",
    "record_id",
    "source_row",
    "order_index",
    "q",
    "a",
    "refs",
    "status",
    "label",
    "review_required",
    "reason",
    "evidence",
    "review_reasons",
    "stage1",
    "stage2",
    "failure",
    "attempt_summary",
    "versions",
)

#: 每条记录一行；`attempt_summary` 只放计数与事实，不放模型输出。
ATTEMPT_SUMMARY_FIELDS: tuple[str, ...] = (
    "total",
    "stage1",
    "stage2",
    "failed",
    "unknown_after_interrupt",
    "simulated",
)


__all__ = [
    "ARTIFACT_EXCEL",
    "ARTIFACT_JSONL",
    "ARTIFACT_KINDS",
    "ARTIFACT_MEDIA_TYPES",
    "ARTIFACT_SUFFIXES",
    "ATTEMPT_SUMMARY_FIELDS",
    "CLASSIFICATION_COLUMNS",
    "EXCEL_CELL_MAX_CHARS",
    "EXCEL_DIGEST_NOTE",
    "EXCEL_DOWNLOAD_STEM",
    "EXCEL_MEDIA_TYPE",
    "EXPORT_CONTRACT_VERSION",
    "EXPORT_SOURCES",
    "ExcelCell",
    "FAILURE_COLUMNS",
    "FORMULA_TRIGGER_PREFIXES",
    "ILLEGAL_CELL_CHARS",
    "ILLEGAL_CELL_CHARS_RE",
    "JSONL_DOWNLOAD_STEM",
    "JSONL_MEDIA_TYPE",
    "JSONL_RECORD_FIELDS",
    "ORDINAL_COLUMN",
    "RECORD_STATUS_TEXT",
    "REVIEW_COLUMNS",
    "REVIEW_HUMAN_COLUMNS",
    "RUN_STATUS_TEXT",
    "SHORT_ID_LENGTH",
    "SHEET_CLASSIFICATION",
    "SHEET_FAILURES",
    "SHEET_ORDER",
    "SHEET_REVIEW",
    "SHEET_SUMMARY",
    "SOURCE_AUTOMATIC",
    "SOURCE_MANUAL",
    "STAGE_TEXT",
    "SUMMARY_COLUMNS",
    "SUMMARY_FIELDS",
    "TEXT_GUARD_PREFIX",
    "TRUNCATION_MARKER",
    "UNPROCESSED_FIELD",
    "cell_location",
    "download_name",
    "excel_cell_text",
    "format_evidence",
    "is_truncatable",
    "needs_text_guard",
    "ordinal",
    "record_status_text",
    "retryable_text",
    "review_required_text",
    "run_status_text",
    "sanitized_cell_location",
    "short_id",
    "stage_text",
    "strip_illegal_cell_chars",
    "truncate_for_excel",
]
