"""S03-07：上传文件与预检结果的**内存态**暂存。

**临时实现**：S03 没有数据库（S04 才做），上传登记与预检快照只保存在 API 进程
内存中，进程重启后 `upload_id`/`validation_id` 全部失效。字段与
[plan/08 §3](../../../plan/08-API接口与数据合同.md) 对齐，S04 换成持久化存储时
接口不变。

原始文件只读保存：服务端生成文件名，原文件名仅作元信息；**不修改原文件内容**。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..excel.reader import file_digest
from ..schemas.qa import ParsedInput


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


@dataclass(frozen=True)
class ValidationRecord:
    """一次预检的不可变快照；同一份上传可因换表而重复预检。"""

    validation_id: str
    upload_id: str
    parsed: ParsedInput


class UploadStore:
    """上传与预检的内存登记表；文件本体写在 ``uploads_dir``。"""

    def __init__(self, uploads_dir: Path) -> None:
        self._dir = uploads_dir
        self._uploads: dict[str, UploadedFile] = {}
        self._validations: dict[str, ValidationRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 上传

    def save(
        self, original_filename: str, stream: BinaryIO, *, max_bytes: int
    ) -> UploadedFile:
        """把上传流写入磁盘并登记；超过 ``max_bytes`` 抛 `UploadTooLargeError`。

        服务端生成存储名，不使用客户端提供的路径。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        upload_id = uuid.uuid4().hex
        suffix = Path(original_filename).suffix.lower() or ".xlsx"
        target = self._dir / f"{upload_id}{suffix}"

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
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        record = UploadedFile(
            upload_id=upload_id,
            path=target,
            original_filename=original_filename,
            size_bytes=size_bytes,
            sha256=file_digest(target),
        )
        with self._lock:
            self._uploads[upload_id] = record
        return record

    def get_upload(self, upload_id: str) -> UploadedFile | None:
        with self._lock:
            return self._uploads.get(upload_id)

    # ------------------------------------------------------------------ 预检

    def put_validation(self, upload_id: str, parsed: ParsedInput) -> ValidationRecord:
        record = ValidationRecord(
            validation_id=uuid.uuid4().hex,
            upload_id=upload_id,
            parsed=parsed,
        )
        with self._lock:
            self._validations[record.validation_id] = record
        return record

    def get_validation(self, validation_id: str) -> ValidationRecord | None:
        with self._lock:
            return self._validations.get(validation_id)


__all__ = [
    "UploadStore",
    "UploadTooLargeError",
    "UploadedFile",
    "ValidationRecord",
]
