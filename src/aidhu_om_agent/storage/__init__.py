"""持久化层：SQLite 连接、短事务、结构版本与迁移。

本包只放「与数据库打交道」的代码。业务规则留在 `agent`，HTTP 形状留在 `api`；
模型请求与文件生成不进写事务（见 [.claude/rules/storage.md]）。

S04-01 提供地基：`database` 模块的连接工厂、`write_transaction`、迁移与版本检查。
各表的读写仓库函数在 S04-02 起按步骤加入。
"""

from __future__ import annotations

from .database import (
    BACKUP_DIRNAME,
    DEFAULT_BUSY_TIMEOUT_MS,
    MIGRATIONS_DIR,
    SCHEMA_VERSION,
    Database,
    DatabaseBusyError,
    DatabaseError,
    MigrationError,
    MigrationResult,
    SchemaVersionError,
    backup_database,
    connect,
    current_version,
    discover_migrations,
    execute_write,
    migrate,
    open_database,
    read_transaction,
    utc_now,
    write_transaction,
)

__all__ = [
    "BACKUP_DIRNAME",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MIGRATIONS_DIR",
    "SCHEMA_VERSION",
    "Database",
    "DatabaseBusyError",
    "DatabaseError",
    "MigrationError",
    "MigrationResult",
    "SchemaVersionError",
    "backup_database",
    "connect",
    "current_version",
    "discover_migrations",
    "execute_write",
    "migrate",
    "open_database",
    "read_transaction",
    "utc_now",
    "write_transaction",
]
