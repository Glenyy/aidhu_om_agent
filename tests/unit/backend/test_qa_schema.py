"""S02-01 的文本与编号归一化纯函数单测。

Excel 端到端样例（`test_excel_reader.py`）无法覆盖 CRLF：openpyxl 在 Windows
写出 XML 时是文本模式，会把 ``\\n`` 再写成 ``\\r\\n``，样例里的 ``\\r\\n`` 到
解析时会变成双换行。真实 Excel 文件存的是 LF，因此换行归一化在这里以纯函数
逐字验证，不依赖 openpyxl 的写出行为。
"""

from __future__ import annotations

import pytest

from aidhu_om_agent.schemas.qa import (
    A_COLUMN,
    ID_COLUMN,
    INPUT_COLUMNS,
    INPUT_CONTRACT_VERSION,
    Q_COLUMN,
    REF_FIELDS,
    cell_to_text,
    is_error_cell,
    is_formula_cell,
    is_numeric_cell,
    missing_required_fields,
    normalize_text,
)
from aidhu_om_agent.schemas.qa import QARecord


# ---------------------------------------------------------------- 换行归一化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb", "a\nb"),  # CRLF → LF
        ("a\rb", "a\nb"),  # 单独 CR → LF
        ("a\nb", "a\nb"),  # LF 不变
        ("a\r\r\nb", "a\n\nb"),  # 已被写成 CRLF 的换行符再加 CR：真实的两次换行
        ("资料一\r\n资料二\r\n资料三", "资料一\n资料二\n资料三"),
    ],
)
def test_normalize_text_unifies_newlines(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  a  ", "a"),
        ("\t x \t", "x"),  # \t 属空白，strip 一并去掉
        ("\n", ""),
        ("   ", ""),
        ("a  b", "a  b"),  # 中间空白保持原样，不压缩
        ("第一行\n  第二行", "第一行\n  第二行"),  # 行内缩进保留
    ],
)
def test_normalize_text_strips_edges_only(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


# ---------------------------------------------------------------- 单元格取值


@pytest.mark.parametrize("value", [None, "", "   ", "\n", "\t"])
def test_blank_cells_become_none(value: object) -> None:
    assert cell_to_text(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, "7"),
        (0, "0"),
        (-3, "-3"),
        (7.0, "7"),  # 整数浮点去掉 .0
        (0.0, "0"),
        (7.5, "7.5"),
        (True, "TRUE"),
        (False, "FALSE"),
        ("007", "007"),  # 文本编号保留前导零，不补也不去
        (" x ", "x"),
        ("1", "1"),
    ],
)
def test_cell_to_text_values(value: object, expected: str) -> None:
    assert cell_to_text(value) == expected


def test_float_without_exact_binary_representation_keeps_value() -> None:
    # 不做四舍五入：非整数浮点按 Python 十进制写出。
    assert cell_to_text(0.1 + 0.2) == "0.30000000000000004"


# ---------------------------------------------------------------- 单元格类型判定


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, True), (7.0, True), (0, True), (True, False), (False, False), ("7", False), (None, False)],
)
def test_is_numeric_cell_excludes_bool(value: object, expected: bool) -> None:
    assert is_numeric_cell(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#N/A", True),
        (" #N/A ", True),
        ("#VALUE!", True),
        ("#DIV/0!", True),
        ("#n/a", False),  # 大小写敏感：Excel 错误值是大写
        ("N/A", False),
        (None, False),
    ],
)
def test_is_error_cell(value: object, expected: bool) -> None:
    assert is_error_cell(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=A1", True),
        ("=CONCATENATE(\"问题\",\"二\")", True),
        ("=", True),
        ("A1", False),
        (7, False),
        (None, False),
    ],
)
def test_is_formula_cell(value: object, expected: bool) -> None:
    assert is_formula_cell(value) is expected


# ---------------------------------------------------------------- 输入合同常量


def test_input_contract_constants() -> None:
    assert INPUT_CONTRACT_VERSION == "1.0"
    assert (ID_COLUMN, Q_COLUMN, A_COLUMN) == ("编号", "q", "a")
    assert REF_FIELDS[0] == "ref1" and REF_FIELDS[-1] == "ref10"
    assert INPUT_COLUMNS == ("编号", "q", "a", *REF_FIELDS)


# ---------------------------------------------------------------- 必填列判定


def make_record(**overrides: object) -> QARecord:
    data: dict[str, object] = {
        "record_id": "1",
        "source_row": 2,
        "order_index": 0,
        "q": "问题",
        "a": "回答",
        "refs": {field: None for field in REF_FIELDS},
    }
    data.update(overrides)
    return QARecord(**data)  # type: ignore[arg-type]


def test_complete_record_has_no_missing_required_fields() -> None:
    assert missing_required_fields(make_record()) == ()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"record_id": None}, (ID_COLUMN,)),
        ({"q": None}, (Q_COLUMN,)),
        ({"a": None}, (A_COLUMN,)),
        ({"record_id": None, "a": None}, (ID_COLUMN, A_COLUMN)),
    ],
)
def test_missing_required_fields_lists_columns(
    overrides: dict[str, object], expected: tuple[str, ...]
) -> None:
    assert missing_required_fields(make_record(**overrides)) == expected


def test_ref_fields_are_not_required() -> None:
    # ref 全空是合法输入：资料不足属业务判断。
    record = make_record(refs={field: None for field in REF_FIELDS})

    assert missing_required_fields(record) == ()
