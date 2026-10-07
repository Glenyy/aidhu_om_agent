"""S04-01：SQLite 连接、短事务、WAL 与结构版本。

依据 [plan/09 §7/§9](../../../../plan/09-数据库与持久化设计.md)：

- 每个 API 操作与 worker 各自使用**独立连接**，不跨进程共用连接对象。
- 每条连接**显式**开启并验证 ``PRAGMA foreign_keys=ON``，不假定默认值。
- 启用 WAL，配置有限的 ``busy_timeout``；写事务一律显式 ``BEGIN IMMEDIATE``
  后及时提交/回滚，**不与隐式事务混用**（连接以 ``isolation_level=None`` 建立，
  即 SQLite 的自动提交模式，事务控制全部由本模块负责）。
- 迁移在 worker 消费前串行执行；**拒绝读取未来版本结构**；迁移前自动备份。

结构版本与迁移脚本分离：本模块只负责「把 ``migrations/*.sql`` 按版本顺序应用到
``schema_migrations`` 记录到的位置」。脚本一旦应用就**不可改写**（按 sha256 校验），
后续结构变更新增 ``002_*.sql``。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: 本程序理解的结构版本；数据库记录到更高版本时拒绝读取（程序比库旧）。
#: 3（S07-03）：新增 `evaluations` 与 `evaluation_records`，评估结果落库。
SCHEMA_VERSION = 3

#: 迁移脚本目录（随包分发，见 pyproject 的 wheel 打包范围）。
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: 默认锁等待；超时后抛 `DatabaseBusyError`，不无限重试。
DEFAULT_BUSY_TIMEOUT_MS = 5000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIGRATION_NAME_RE = re.compile(r"^(?P<version>\d+)_[A-Za-z0-9_]+\.sql$")

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version       INTEGER PRIMARY KEY,
    script_sha256 TEXT NOT NULL,
    applied_at    TEXT NOT NULL
)
"""


class DatabaseError(RuntimeError):
    """数据库层错误基类。"""


class DatabaseBusyError(DatabaseError):
    """锁等待超时（并发写入）；调用方可识别并按可恢复错误处理。"""


class MigrationError(DatabaseError):
    """迁移脚本缺失、被改写或执行失败。"""


class SchemaVersionError(DatabaseError):
    """数据库结构版本高于本程序理解的版本。"""


@dataclass(frozen=True)
class MigrationFile:
    """一个待应用的迁移脚本。"""

    version: int
    path: Path
    sql: str
    sha256: str


@dataclass(frozen=True)
class MigrationResult:
    """一次迁移的结果；``applied_now`` 为空表示库已是最新。"""

    version: int
    applied_now: tuple[int, ...]
    backup_path: Path | None


def utc_now() -> str:
    """数据库时间戳统一用 UTC ISO 8601（含偏移），可字典序比较。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _translate_busy(exc: sqlite3.Error, context: str) -> DatabaseError:
    """把 SQLite 的锁错误翻成可识别错误，其它错误原样返回。"""
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return DatabaseBusyError(f"{context}：数据库忙（{exc}）")
    return DatabaseError(f"{context}：{exc}")


def connect(
    path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """打开一条已配置好的连接；调用方负责关闭。

    ``isolation_level=None`` 表示由本模块显式控制事务；``row_factory`` 用
    ``sqlite3.Row`` 便于按列名取值。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row

    # 外键必须显式开启并**回读验证**：SQLite 默认关闭，且该 PRAGMA 是每连接生效。
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise DatabaseError("无法开启 foreign_keys：连接未通过回读验证")

    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        connection.close()
        raise DatabaseError(f"无法启用 WAL（实际 journal_mode={mode!r}）")

    # WAL 下 NORMAL 是常规搭配：提交不强制 fsync，崩溃可能丢最后若干事务，
    # 但不会损坏库；本工具是本地单机批处理，接受该取舍。
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return connection


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一个短的写事务：``BEGIN IMMEDIATE`` → 提交，异常则回滚。

    **不嵌套**：连接已在事务中时直接报错，避免隐式事务与显式 BEGIN 混用造成的
    「提交了但以为没提交」。模型请求与文件生成必须留在事务外（见 storage 规则）。
    """
    if connection.in_transaction:
        raise DatabaseError("写事务不可嵌套：当前连接已在事务中")
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise _translate_busy(exc, "开启写事务失败") from exc

    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        try:
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise _translate_busy(exc, "提交写事务失败") from exc


@contextmanager
def read_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一个读事务：块内多次 SELECT 落在**同一快照**上（[plan/08 §5]）。

    WAL 下第一次读才建立快照，所以这里用延迟的 ``BEGIN`` 而不是
    ``BEGIN IMMEDIATE``——读接口不该去抢写锁、更不该被写事务挡在门外。结束时一律
    ``ROLLBACK``：读事务没有需要提交的东西，用回滚释放快照最不容易写错。

    详情页的计数、失败列表与当前任务必须来自同一时刻，否则会出现「failed=1 但
    失败列表为空」这类自相矛盾的画面；进度条同理。
    """
    if connection.in_transaction:
        raise DatabaseError("读事务不可嵌套：当前连接已在事务中")
    try:
        connection.execute("BEGIN")
    except sqlite3.Error as exc:
        raise _translate_busy(exc, "开启读事务失败") from exc

    try:
        yield connection
    finally:
        connection.rollback()


def execute_write(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()
) -> sqlite3.Cursor:
    """在独立短事务里执行一条写语句，返回游标。

    单条语句的便利入口；多条语句需要同一事务时用 `write_transaction`。
    """
    with write_transaction(connection) as conn:
        return conn.execute(sql, parameters)


def current_version(connection: sqlite3.Connection) -> int:
    """库里已应用的最高结构版本；全新的库为 0。"""
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    value = connection.execute(
        "SELECT MAX(version) AS version FROM schema_migrations"
    ).fetchone()["version"]
    return int(value) if value is not None else 0


def discover_migrations(directory: Path | None = None) -> tuple[MigrationFile, ...]:
    """按版本升序读取迁移脚本；版本重复或命名非法即报错。"""
    target = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not target.is_dir():
        raise MigrationError(f"迁移脚本目录不存在：{target}")

    files: list[MigrationFile] = []
    seen: dict[int, Path] = {}
    for path in sorted(target.glob("*.sql")):
        match = _MIGRATION_NAME_RE.match(path.name)
        if match is None:
            raise MigrationError(f"迁移脚本命名不符合 <版本>_<名称>.sql：{path.name}")
        version = int(match.group("version"))
        if version in seen:
            raise MigrationError(f"迁移版本重复：{version}（{seen[version].name} 与 {path.name}）")
        seen[version] = path
        sql = path.read_text(encoding="utf-8")
        files.append(
            MigrationFile(
                version=version,
                path=path,
                sql=sql,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    if not files:
        raise MigrationError(f"迁移脚本目录为空：{target}")
    files.sort(key=lambda item: item.version)
    return tuple(files)


def backup_database(connection: sqlite3.Connection, target: Path) -> Path:
    """用 SQLite 备份接口取一致副本。

    直接复制处于 WAL 模式的主文件会漏掉尚未 checkpoint 的内容，因此一律走
    `sqlite3.Connection.backup`。备份文件**不自动删除**（[plan/09 §1]）。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(str(target))
    try:
        connection.backup(destination)
    except sqlite3.Error as exc:
        raise DatabaseError(f"备份数据库失败：{exc}") from exc
    finally:
        destination.close()
    return target


def migration_backup_path(backup_dir: Path, version: int) -> Path:
    """迁移前备份的固定命名：同一秒内重复迁移也不会互相覆盖。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(backup_dir) / f"state-v{version}-{stamp}.sqlite3"


def _apply(connection: sqlite3.Connection, migration: MigrationFile) -> None:
    """应用一个脚本，并把版本登记**放进同一个事务**。

    `executescript` 不支持参数绑定，因此把三个常量内插进脚本：``version`` 是整数、
    ``sha256`` 先按十六进制正则校验、时间戳由本模块生成——三者都不可能含引号。
    这样做是为了让「建表/改表」与「登记版本」原子提交：进程若在两者之间中断，
    下一次启动会重跑同一脚本并因表已存在而失败，库就卡住了。
    """
    if not _SHA256_RE.match(migration.sha256):  # pragma: no cover - 内部不变量
        raise MigrationError(f"脚本摘要格式异常：{migration.path.name}")

    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_migrations (version, script_sha256, applied_at) VALUES "
        f"({migration.version}, '{migration.sha256}', '{utc_now()}');\n"
        "COMMIT;\n"
    )
    try:
        connection.executescript(script)
    except sqlite3.Error as exc:
        # 脚本自带 BEGIN 且未走到 COMMIT 时事务仍然打开，必须显式回滚。
        connection.rollback()
        raise MigrationError(f"应用迁移 {migration.path.name} 失败：{exc}") from exc


def migrate(
    connection: sqlite3.Connection, *, backup_dir: Path | None = None
) -> MigrationResult:
    """把库升到最新结构版本；已是最新则不做任何写入。

    ``backup_dir`` 给出时，**在有待应用脚本且库非空**的情况下先备份一次。
    未来版本（库或脚本高于本程序）一律拒绝，不尝试降级或猜测。
    """
    connection.execute(_CREATE_MIGRATIONS_TABLE)
    applied = {
        int(row["version"]): str(row["script_sha256"])
        for row in connection.execute("SELECT version, script_sha256 FROM schema_migrations")
    }

    if applied and max(applied) > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"数据库结构版本 {max(applied)} 高于本程序支持的 {SCHEMA_VERSION}；"
            "请使用对应版本的程序，或先备份再用兼容程序处理"
        )

    files = discover_migrations()
    by_version = {item.version: item for item in files}

    unknown = [item.version for item in files if item.version > SCHEMA_VERSION]
    if unknown:
        raise SchemaVersionError(
            f"迁移脚本版本 {unknown} 高于本程序支持的 {SCHEMA_VERSION}；程序版本过旧"
        )

    missing = sorted(set(applied) - set(by_version))
    if missing:
        raise MigrationError(f"已应用的迁移脚本在磁盘上找不到：{missing}")

    for version, recorded in sorted(applied.items()):
        actual = by_version[version].sha256
        if actual != recorded:
            raise MigrationError(
                f"迁移 {version} 已被改写（记录 {recorded[:12]}…，实际 {actual[:12]}…）；"
                "已应用的脚本不可修改，请新增一个版本"
            )

    pending = [item for item in files if item.version not in applied]
    if not pending:
        return MigrationResult(version=max(applied) if applied else 0, applied_now=(), backup_path=None)

    backup_path: Path | None = None
    if backup_dir is not None and applied:
        backup_path = backup_database(
            connection, migration_backup_path(Path(backup_dir), max(applied))
        )

    for migration in pending:
        _apply(connection, migration)

    return MigrationResult(
        version=current_version(connection),
        applied_now=tuple(item.version for item in pending),
        backup_path=backup_path,
    )


def open_database(path: Path, *, backup_dir: Path | None = None) -> sqlite3.Connection:
    """建立连接、迁移到最新并校验版本；失败时关闭连接再抛出。"""
    connection = connect(path)
    try:
        migrate(connection, backup_dir=backup_dir)
        version = current_version(connection)
        if version > SCHEMA_VERSION:  # pragma: no cover - migrate 已拦截
            raise SchemaVersionError(f"数据库结构版本 {version} 不受支持")
    except BaseException:
        connection.close()
        raise
    return connection


#: 迁移前备份的默认子目录名；相对 `runs` 数据库所在目录（默认即 `data/backups`）。
BACKUP_DIRNAME = "backups"


class Database:
    """数据库位置与连接工厂（S04-02）。

    按 [plan/09 §7](../../../../plan/09-数据库与持久化设计.md)，**每个 API 操作与
    worker 各自使用独立连接**，不跨线程/进程共用一个连接对象。因此本类不持有连接，
    只持有路径：

    - `initialize()`：启动时调用一次，把库迁移到最新（多进程同时启动也安全）。
    - `connect()`：每次操作用一次，调用方负责关闭。

    备份目录固定为数据库同级的 `backups/`，不新增配置键（见 S04 阶段文档 §0.3）。
    """

    def __init__(
        self,
        path: Path,
        *,
        backup_dir: Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = (
            Path(backup_dir)
            if backup_dir is not None
            else self.path.parent / BACKUP_DIRNAME
        )
        self.busy_timeout_ms = int(busy_timeout_ms)

    def connect(self) -> sqlite3.Connection:
        """一条已配置 PRAGMA 的连接；**不**做迁移检查。"""
        return connect(self.path, busy_timeout_ms=self.busy_timeout_ms)

    def initialize(self) -> MigrationResult:
        """迁移到最新结构版本并返回结果；已是最新时不产生新备份。"""
        connection = self.connect()
        try:
            return migrate(connection, backup_dir=self.backup_dir)
        finally:
            connection.close()

    def __repr__(self) -> str:  # pragma: no cover - 便于日志排查
        return f"Database(path={str(self.path)!r})"


__all__ = [
    "BACKUP_DIRNAME",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MIGRATIONS_DIR",
    "SCHEMA_VERSION",
    "Database",
    "DatabaseBusyError",
    "DatabaseError",
    "MigrationError",
    "MigrationFile",
    "MigrationResult",
    "SchemaVersionError",
    "backup_database",
    "connect",
    "current_version",
    "discover_migrations",
    "execute_write",
    "migrate",
    "migration_backup_path",
    "open_database",
    "utc_now",
    "write_transaction",
]
