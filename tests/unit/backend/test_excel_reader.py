"""S02-01—S02-03、S02-05 的输入解析与预检单测。

覆盖：选表顺序、缺列/重名列/重复编号/超限/无有效记录阻断、部分失败与
统计恒等式、编号归一化、换行统一、ref 全空合法、错误值与公式无缓存值、
额外列忽略、来源行与顺序保留、原文件不被改写。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aidhu_om_agent.config import LimitsConfig
from aidhu_om_agent.excel.reader import (
    BLOCKER_DUPLICATE_COLUMNS,
    BLOCKER_DUPLICATE_RECORD_ID,
    BLOCKER_FILE_TOO_LARGE,
    BLOCKER_MISSING_COLUMNS,
    BLOCKER_NO_VALID_RECORDS,
    BLOCKER_SHEET_AMBIGUOUS,
    BLOCKER_TOO_MANY_RECORDS,
    WARNING_EXTRA_COLUMNS,
    WARNING_NUMERIC_RECORD_ID,
    InputReadError,
    file_digest,
    precheck,
    select_sheet,
)
from aidhu_om_agent.schemas.qa import INPUT_COLUMNS, REF_FIELDS

from fixtures.excel_samples import (
    all_invalid_workbook,
    duplicated_header_workbook,
    duplicated_id_workbook,
    empty_refs_workbook,
    error_value_workbook,
    extra_column_workbook,
    formula_without_cache_workbook,
    missing_column_workbook,
    multi_sheet_workbook,
    normal_workbook,
    numeric_id_workbook,
    oversized_workbook,
    partial_failure_workbook,
    single_sheet_workbook,
    text_id_leading_zero_workbook,
)

GENEROUS = LimitsConfig(max_upload_bytes=52428800, max_records=1000)


def blocker_codes(parsed) -> set[str]:
    return {blocker.code for blocker in parsed.report.blockers}


def warning_codes(parsed) -> set[str]:
    return {warning.code for warning in parsed.report.warnings}


# ---------------------------------------------------------------- 输入合同常量


def test_input_contract_has_thirteen_columns() -> None:
    assert len(INPUT_COLUMNS) == 13
    assert len(REF_FIELDS) == 10
    assert INPUT_COLUMNS[:3] == ("编号", "q", "a")
    assert INPUT_COLUMNS[3:] == REF_FIELDS


# ---------------------------------------------------------------- 正常解析


def test_normal_workbook_counts_and_order(tmp_path: Path) -> None:
    parsed = precheck(normal_workbook(tmp_path / "ok.xlsx"), limits=GENEROUS)
    report = parsed.report

    assert report.status == "passed"
    assert report.sheet_name == "QA_REF"
    assert report.input_contract_version == "1.0"
    assert report.counts.total == 3
    assert report.counts.valid == 3
    assert report.counts.input_invalid == 0
    assert report.counts.skipped_blank_rows == 1
    # 空行另计：total 只统计非空记录。
    assert report.counts.total == report.counts.valid + report.counts.input_invalid

    assert [record.record_id for record in parsed.records] == ["1", "2", "A-003"]
    assert [record.source_row for record in parsed.records] == [2, 3, 4]
    assert [record.order_index for record in parsed.records] == [0, 1, 2]


def test_record_keeps_all_ten_refs_and_newline_normalized(tmp_path: Path) -> None:
    parsed = precheck(normal_workbook(tmp_path / "ok.xlsx"), limits=GENEROUS)
    first, second, third = parsed.records

    assert tuple(first.refs) == REF_FIELDS
    assert first.ref_text("ref1") == "资料一"
    assert first.ref_text("ref10") is None
    # ref 为空合法，不是输入失败。
    assert all(value is None for value in second.refs.values())
    # \r\n 统一成 \n。
    assert third.ref_text("ref1") == "资料三第一行\n资料三第二行"


def test_valid_and_invalid_split(tmp_path: Path) -> None:
    parsed = precheck(partial_failure_workbook(tmp_path / "partial.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "passed"
    assert parsed.report.counts.total == 4
    assert parsed.report.counts.valid == 1
    assert parsed.report.counts.input_invalid == 3
    # 全空行与仅空白行都算空行。
    assert parsed.report.counts.skipped_blank_rows == 2

    assert [record.record_id for record in parsed.valid_records()] == ["1"]
    assert [record.source_row for record in parsed.invalid_records()] == [3, 4, 5]

    reasons = {error.source_row: error.reason for error in parsed.report.row_errors}
    assert "编号：缺失或为空" in reasons[3]
    assert "q：缺失或为空" in reasons[4]
    assert "a：缺失或为空" in reasons[5]


def test_failed_record_does_not_fabricate_q_or_a(tmp_path: Path) -> None:
    parsed = precheck(partial_failure_workbook(tmp_path / "partial.xlsx"), limits=GENEROUS)
    failed = {record.source_row: record for record in parsed.invalid_records()}

    assert failed[4].q is None and failed[4].a == "回答三"
    assert failed[5].a is None and failed[5].q == "问题四"
    # 失败记录仍保留可读编号与顺序，便于按来源行追踪。
    assert failed[4].record_id == "3"
    assert failed[4].order_index == 2


# ---------------------------------------------------------------- 编号归一化


def test_numeric_record_id_drops_trailing_zero(tmp_path: Path) -> None:
    parsed = precheck(numeric_id_workbook(tmp_path / "num.xlsx"), limits=GENEROUS)

    assert [record.record_id for record in parsed.records] == ["7", "8"]
    assert WARNING_NUMERIC_RECORD_ID in warning_codes(parsed)


def test_text_record_id_keeps_leading_zeros(tmp_path: Path) -> None:
    parsed = precheck(text_id_leading_zero_workbook(tmp_path / "zero.xlsx"), limits=GENEROUS)

    assert [record.record_id for record in parsed.records] == ["007", "008"]
    # 文本编号不触发数值提示。
    assert WARNING_NUMERIC_RECORD_ID not in warning_codes(parsed)


# ---------------------------------------------------------------- 选表


def test_select_sheet_prefers_qa_ref() -> None:
    selection = select_sheet([("Other", True), ("QA_REF", True)])

    assert selection.name == "QA_REF"


def test_select_sheet_uses_only_visible_sheet() -> None:
    selection = select_sheet([("Only", True), ("Hidden", False)])

    assert selection.name == "Only"


def test_select_sheet_refuses_multiple_visible_sheets() -> None:
    selection = select_sheet([("A", True), ("B", True)])

    assert selection.name is None
    assert "无法自动确定" in selection.reason
    assert selection.candidates == (("A", True), ("B", True))


def test_select_sheet_missing_requested_name() -> None:
    selection = select_sheet([("A", True)], requested="QA_REF")

    assert selection.name is None
    assert "没有工作表" in selection.reason


def test_explicit_sheet_is_honoured(tmp_path: Path) -> None:
    path = single_sheet_workbook(tmp_path / "other.xlsx")
    parsed = precheck(path, "Sheet1", limits=GENEROUS)

    assert parsed.report.status == "passed"
    assert parsed.report.sheet_name == "Sheet1"


def test_ambiguous_sheet_blocks_without_counts(tmp_path: Path) -> None:
    parsed = precheck(multi_sheet_workbook(tmp_path / "multi.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_SHEET_AMBIGUOUS}
    assert parsed.report.sheet_name is None
    # 未扫描数据行时计数不可统计。
    assert parsed.report.counts.total is None
    assert parsed.records == ()


# ---------------------------------------------------------------- 批次级阻断


def test_missing_column_blocks(tmp_path: Path) -> None:
    parsed = precheck(missing_column_workbook(tmp_path / "miss.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_MISSING_COLUMNS}
    assert parsed.report.blockers[0].field == "ref10"
    assert parsed.report.counts.total is None
    assert parsed.records == ()


def test_duplicated_header_blocks(tmp_path: Path) -> None:
    parsed = precheck(duplicated_header_workbook(tmp_path / "dupcol.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_DUPLICATE_COLUMNS}
    assert parsed.records == ()


def test_duplicated_record_id_blocks_with_source_rows(tmp_path: Path) -> None:
    parsed = precheck(duplicated_id_workbook(tmp_path / "dupid.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_DUPLICATE_RECORD_ID}
    blocker = parsed.report.blockers[0]
    assert blocker.source_rows == (2, 4)
    assert "1" in blocker.field
    # 已完整扫描，计数仍可给出。
    assert parsed.report.counts.total == 3
    assert parsed.records == ()


def test_too_many_records_blocks(tmp_path: Path) -> None:
    path = oversized_workbook(tmp_path / "many.xlsx", 5)
    parsed = precheck(path, limits=LimitsConfig(max_upload_bytes=52428800, max_records=3))

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_TOO_MANY_RECORDS}
    assert parsed.report.counts.total == 5
    assert parsed.records == ()


def test_file_too_large_blocks_before_reading(tmp_path: Path) -> None:
    path = normal_workbook(tmp_path / "ok.xlsx")
    parsed = precheck(path, limits=LimitsConfig(max_upload_bytes=100, max_records=1000))

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_FILE_TOO_LARGE}
    assert parsed.report.counts.total is None
    assert parsed.report.file_size_bytes == path.stat().st_size


def test_no_valid_records_blocks(tmp_path: Path) -> None:
    parsed = precheck(all_invalid_workbook(tmp_path / "none.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "blocked"
    assert blocker_codes(parsed) == {BLOCKER_NO_VALID_RECORDS}
    assert parsed.report.counts.valid == 0
    assert parsed.report.counts.total == 1


# ---------------------------------------------------------------- 单元格不可读


def test_error_value_cell_marks_input_invalid(tmp_path: Path) -> None:
    parsed = precheck(error_value_workbook(tmp_path / "err.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "passed"
    assert parsed.report.counts.valid == 1
    assert parsed.report.counts.input_invalid == 1
    reason = parsed.report.row_errors[0].reason
    assert "q：不可读取" in reason and "#N/A" in reason
    assert "ref1：不可读取" in reason and "#VALUE!" in reason


def test_formula_without_cache_marks_input_invalid(tmp_path: Path) -> None:
    parsed = precheck(
        formula_without_cache_workbook(tmp_path / "formula.xlsx"), limits=GENEROUS
    )

    assert parsed.report.status == "passed"
    assert parsed.report.counts.input_invalid == 1
    reason = parsed.report.row_errors[0].reason
    assert "q：不可读取" in reason and "公式单元格没有缓存值" in reason


# ---------------------------------------------------------------- 提示与边界


def test_extra_column_is_ignored_with_warning(tmp_path: Path) -> None:
    parsed = precheck(extra_column_workbook(tmp_path / "extra.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "passed"
    assert WARNING_EXTRA_COLUMNS in warning_codes(parsed)
    # 额外列不进记录：refs 仍只有固定十项。
    assert tuple(parsed.records[0].refs) == REF_FIELDS


def test_empty_refs_are_valid(tmp_path: Path) -> None:
    parsed = precheck(empty_refs_workbook(tmp_path / "empty.xlsx"), limits=GENEROUS)

    assert parsed.report.status == "passed"
    assert parsed.report.counts.valid == 1
    assert parsed.report.row_errors == ()


def test_missing_file_raises_read_error(tmp_path: Path) -> None:
    with pytest.raises(InputReadError):
        precheck(tmp_path / "不存在.xlsx", limits=GENEROUS)


def test_source_file_is_not_modified(tmp_path: Path) -> None:
    path = normal_workbook(tmp_path / "ok.xlsx")
    before = file_digest(path)

    parsed = precheck(path, limits=GENEROUS)

    assert file_digest(path) == before
    assert parsed.report.file_sha256 == before


def test_blank_trailing_row_is_not_a_record(tmp_path: Path) -> None:
    parsed = precheck(normal_workbook(tmp_path / "ok.xlsx"), limits=GENEROUS)

    assert parsed.report.counts.skipped_blank_rows == 1
    assert all(record.record_id is not None for record in parsed.records)
