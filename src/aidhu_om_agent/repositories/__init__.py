"""数据访问层：SQL 只写在这里（S04-02 起）。

分工：

- 本层负责**行与事务**——把表行读成 dataclass、把 dataclass 写成行，并保证同一次
  提交要么整体生效要么整体回滚。
- 业务规则（谁能创建批次、哪些记录要重开预算）留在 `services`。
- HTTP 形状留在 `api`。

约定：

- 所有写操作都通过 `storage.write_transaction`（`BEGIN IMMEDIATE`，短事务）。
- **模型请求与文件生成不进事务**（见 [.claude/rules/storage.md]）。
- JSON 列一律用 ``ensure_ascii=False`` 保存，便于人工核对。
"""

from __future__ import annotations

__all__: list[str] = []
