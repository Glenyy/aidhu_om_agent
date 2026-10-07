"""上传与预检的持久化登记（S04-02）。

S03-07 的内存实现在本阶段换成 SQLite：`uploads`／`input_validations` 各占一行，
**进程重启后 upload_id / validation_id 仍然有效**；HTTP 接口形状不变
（[plan/08 §3](../../../plan/08-API接口与数据合同.md)）。

原始文件只读保存：服务端生成文件名，原文件名仅作元信息；**不修改原文件内容**。
文件**先完整写入再登记**；写入、读取工作表清单或登记任一步失败都删除刚写入的文件，
不留孤儿。

预检快照保存的是**报告**（`PrecheckReport`），不重复保存记录行：记录属于批次
（plan/09 §3）。需要完整 `ParsedInput` 时由 `services.batches.load_parsed_input()`
用**未变的原文件**重算并与快照核对，见该函数的说明。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..config import LimitsConfig
from ..excel.reader import file_digest, sheet_catalog
from ..repositories import runs as runs_repo
from ..schemas.qa import ParsedInput, PrecheckReport
from ..storage import Database, utc_now, write_transaction


class UploadTooLargeError(Exception):
    """写入过程中超过体积上限；已写入的部分文件会被删除。"""


@dataclass(frozen=True)
class UploadedFile:
    """一次上传的登记信息。"""

    upload_id: str
    path: Path
    original_filename: str
    size_bytes: int
    sha256: str
    sheets: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True)
class ValidationRecord:
    """一次预检的不可变快照；同一份上传可因换表而重复预检。"""

    validation_id: str
    upload_id: str
    report: PrecheckReport

    @property
    def status(self) -> str:
        return self.report.status


class UploadStore:
    """上传与预检的持久化登记；文件本体写在 ``uploads_dir``。"""

    def __init__(self, database: Database, uploads_dir: Path) -> None:
        self._db = database
        self._dir = Path(uploads_dir)
        # 同进程内的并发上传仍需串行化文件名与登记；跨进程由数据库约束保证。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 上传

    def save(
        self, original_filename: str, stream: BinaryIO, *, limits: LimitsConfig
    ) -> UploadedFile:
        """把上传流写入磁盘并登记；超过体积上限抛 `UploadTooLargeError`。

        服务端生成存储名，不使用客户端提供的路径。

        S06-02 起取整个 ``limits`` 而不是单个 ``max_bytes``：工作表清单读取里带着
        **解压总量**与**可见工作表数**两项防护，它们都要跟着同一份配置走（解压上限
        是 ``max_upload_bytes`` 的固定倍数，见 `excel.reader.MAX_EXPANDED_RATIO`）。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        upload_id = uuid.uuid4().hex
        suffix = Path(original_filename).suffix.lower() or ".xlsx"
        target = self._dir / f"{upload_id}{suffix}"
        max_bytes = limits.max_upload_bytes

        size_bytes = 0
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        raise UploadTooLargeError(
                            f"文件超过上限 {max_bytes} 字节；不截取内容"
                        )
                    handle.write(chunk)

            digest = file_digest(target)
            sheets = tuple(sheet_catalog(target, limits=limits))
        except BaseException:
            # 写入超限、不是 xlsx、规模防护命中——三种情况都还没登记，文件留着只会
            # 占着上传目录（S06-02 补：工作表清单这一步原先在 try 之外，读不出清单
            # 时会留下孤儿文件）。
            target.unlink(missing_ok=True)
            raise

        record = UploadedFile(
            upload_id=upload_id,
            path=target,
            original_filename=original_filename,
            size_bytes=size_bytes,
            sha256=digest,
            sheets=sheets,
        )
        try:
            with self._lock:
                connection = self._db.connect()
                try:
                    with write_transaction(connection) as conn:
                        runs_repo.insert_upload(
                            conn,
                            upload_id=upload_id,
                            original_filename=original_filename,
                            relative_path=target.name,
                            sha256=digest,
                            size_bytes=size_bytes,
                            sheets=sheets,
                            created_at=utc_now(),
                        )
                finally:
                    connection.close()
        except BaseException:
            # 登记失败不留孤儿文件：下次上传会生成新的 upload_id。
            target.unlink(missing_ok=True)
            raise
        return record

    def get_upload(self, upload_id: str) -> UploadedFile | None:
        connection = self._db.connect()
        try:
            row = runs_repo.get_upload(connection, upload_id)
        finally:
            connection.close()
        if row is None:
            return None
        path = row.path(self._dir)
        if not path.is_file():
            return None
        return UploadedFile(
            upload_id=row.upload_id,
            path=path,
            original_filename=row.original_filename,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            sheets=row.sheets,
        )

    # ------------------------------------------------------------------ 预检

    def put_validation(self, upload_id: str, parsed: ParsedInput) -> ValidationRecord:
        """保存一次预检报告；**不覆盖**同一上传的历史预检。"""
        validation_id = uuid.uuid4().hex
        with self._lock:
            connection = self._db.connect()
            try:
                with write_transaction(connection) as conn:
                    runs_repo.insert_input_validation(
                        conn,
                        validation_id=validation_id,
                        upload_id=upload_id,
                        report=parsed.report,
                        created_at=utc_now(),
                    )
            finally:
                connection.close()
        return ValidationRecord(
            validation_id=validation_id, upload_id=upload_id, report=parsed.report
        )

    def get_validation(self, validation_id: str) -> ValidationRecord | None:
        """读取预检报告；**不含记录行**（记录属于批次）。"""
        connection = self._db.connect()
        try:
            row = runs_repo.get_input_validation(connection, validation_id)
        finally:
            connection.close()
        if row is None:
            return None
        return ValidationRecord(
            validation_id=row.validation_id,
            upload_id=row.upload_id,
            report=row.report,
        )


__all__ = [
    "UploadStore",
    "UploadTooLargeError",
    "UploadedFile",
    "ValidationRecord",
]
