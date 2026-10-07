"""S06-02 的上传规模防护接口测试。

三层防护各自走真实接口：

- **解压总量**与**可见工作表数**在上传时就被拒（422 `BAD_WORKBOOK`）；
- **单元格数**需要先选中表，所以落在预检那一步；
- `413 FILE_TOO_LARGE` 仍**只**用于压缩包字节数，两种错误不混用。

每条都要能核对**实际值与上限**，且被拒的上传不留孤儿文件。

全部使用合成工作簿与本地 SQLite，**零真实模型调用**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.excel_samples import (
    heavy_text_workbook,
    many_cells_workbook,
    many_sheets_workbook,
    normal_workbook,
)
from test_api_s03 import make_config, upload_workbook, validate

from aidhu_om_agent.api.app import create_app
from aidhu_om_agent.excel.reader import MAX_EXPANDED_RATIO, MAX_SHEET_CELLS, MAX_VISIBLE_SHEETS

#: 64 KiB 上传上限：重文本样例压缩后约 16 KB（过 413 检查）、解压后约 3 MB。
TIGHT_UPLOAD_BYTES = 64 * 1024


def error_of(response) -> dict[str, object]:
    return response.json()["error"]


def stored_files(config) -> list[Path]:
    directory = config.paths.uploads
    return list(directory.iterdir()) if directory.exists() else []


# ---------------------------------------------------------------- 解压总量


def test_expanded_size_hit_is_422_with_actual_and_limit(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_upload_bytes=TIGHT_UPLOAD_BYTES)
    client = TestClient(create_app(config))
    path = heavy_text_workbook(tmp_path / "heavy.xlsx")
    budget = MAX_EXPANDED_RATIO * TIGHT_UPLOAD_BYTES

    assert path.stat().st_size < TIGHT_UPLOAD_BYTES  # 压缩包本身合规

    response = upload_workbook(client, path)

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "BAD_WORKBOOK"
    assert str(budget) in error["message"]  # 上限
    assert str(TIGHT_UPLOAD_BYTES) in error["message"]  # 上限是怎么算出来的
    assert str(MAX_EXPANDED_RATIO) in error["message"]
    # 实际值：解压后的字节数，比上限大
    actual = int(str(error["message"]).split("合计 ")[1].split(" 字节")[0])
    assert actual > budget


def test_expanded_size_rejection_leaves_no_file(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_upload_bytes=TIGHT_UPLOAD_BYTES)
    client = TestClient(create_app(config))

    upload_workbook(client, heavy_text_workbook(tmp_path / "heavy.xlsx"))

    assert stored_files(config) == []


def test_long_text_within_limits_is_accepted(tmp_path: Path) -> None:
    """同一份文件在宽松上限下正常上传：防护跟配置联动，不是一刀切。"""
    config = make_config(tmp_path)
    client = TestClient(create_app(config))

    response = upload_workbook(client, heavy_text_workbook(tmp_path / "heavy.xlsx", rows=100))

    assert response.status_code == 201
    assert response.json()["data"]["size_bytes"] > 0


# ---------------------------------------------------------------- 工作表数


def test_visible_sheet_hit_is_422_with_actual_and_limit(tmp_path: Path) -> None:
    client = TestClient(create_app(make_config(tmp_path)))
    path = many_sheets_workbook(tmp_path / "sheets.xlsx", sheets=MAX_VISIBLE_SHEETS + 1)

    response = upload_workbook(client, path)

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "BAD_WORKBOOK"
    assert str(MAX_VISIBLE_SHEETS + 1) in error["message"]  # 实际值
    assert str(MAX_VISIBLE_SHEETS) in error["message"]  # 上限


def test_sheet_count_rejection_leaves_no_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = TestClient(create_app(config))

    upload_workbook(
        client, many_sheets_workbook(tmp_path / "sheets.xlsx", sheets=MAX_VISIBLE_SHEETS + 1)
    )

    assert stored_files(config) == []


# ---------------------------------------------------------------- 单元格数


def test_sheet_cell_hit_is_rejected_at_validation(tmp_path: Path) -> None:
    """上传成功（文件是用户的输入），预检时按选中表判超限。"""
    config = make_config(tmp_path)
    client = TestClient(create_app(config))
    path = many_cells_workbook(tmp_path / "cells.xlsx", row=1000, column=600)

    upload = upload_workbook(client, path)

    assert upload.status_code == 201  # 上传这一步还没有「选中的表」
    response = validate(client, upload.json()["data"]["upload_id"])

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "BAD_WORKBOOK"
    assert str(1000 * 600) in error["message"]  # 实际值（约数）
    assert str(MAX_SHEET_CELLS) in error["message"]  # 上限
    assert len(stored_files(config)) == 1  # 是用户自己的输入，不删


# ---------------------------------------------------------------- 413 与 422 不混用


def test_compressed_size_still_reports_413_alone(tmp_path: Path) -> None:
    """压缩包超限仍是 413 `FILE_TOO_LARGE`，且不走 `BAD_WORKBOOK` 那条分支。"""
    client = TestClient(create_app(make_config(tmp_path, max_upload_bytes=64)))

    response = upload_workbook(client, normal_workbook(tmp_path / "ok.xlsx"))

    assert response.status_code == 413
    error = error_of(response)
    assert error["code"] == "FILE_TOO_LARGE"
    assert "BAD_WORKBOOK" not in str(error)


def test_both_scales_can_be_hit_by_different_files(tmp_path: Path) -> None:
    """同一个上限下：压缩包超限报 413，解压超限报 422——两条路各自独立。"""
    config = make_config(tmp_path, max_upload_bytes=TIGHT_UPLOAD_BYTES)
    client = TestClient(create_app(config))

    assert upload_workbook(client, heavy_text_workbook(tmp_path / "heavy.xlsx")).status_code == 422

    pinned = make_config(tmp_path, max_upload_bytes=1024)
    pinned_client = TestClient(create_app(pinned))
    response = upload_workbook(pinned_client, normal_workbook(tmp_path / "ok.xlsx"))
    assert response.status_code == 413


def test_ordinary_workbook_is_unaffected(tmp_path: Path) -> None:
    """防护没有误伤常规输入：正常样例照常上传、照常通过预检。"""
    client = TestClient(create_app(make_config(tmp_path)))
    path = normal_workbook(tmp_path / "ok.xlsx")

    upload = upload_workbook(client, path)
    assert upload.status_code == 201
    data = validate(client, upload.json()["data"]["upload_id"]).json()["data"]
    assert data["status"] == "passed"
    assert data["counts"]["valid"] == 3
