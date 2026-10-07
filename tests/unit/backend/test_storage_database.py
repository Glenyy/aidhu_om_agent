"""S04-01 数据库初始化、事务与结构版本的单测。

覆盖：连接 PRAGMA（外键回读验证、WAL、busy_timeout）、短事务的提交/回滚/禁止嵌套、
结构版本与迁移（幂等、迁移前备份、脚本不可改写、缺失脚本、未来版本拒绝、
失败迁移整体回滚），以及 001_init.sql 的 CHECK 与部分唯一索引是否真的生效。

这些断言针对**数据库层约束**：它们必须能拦住绕过服务层的写入，否则计划里
「约束分两层」的第一层就是空的。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from aidhu_om_agent import storage
from aidhu_om_agent.storage import database

# 001_init.sql 建出的表（不含迁移登记表本身）。
EXPECTED_TABLES = (
    "artifacts",
    "call_attempts",
    "exports",
    "idempotency_keys",
    "input_validations",
    "jobs",
    "records",
    "runs",
    "runtime_state",
    "stage_campaigns",
    "stage_results",
    "uploads",
)

RUN_INSERT = """
INSERT INTO runs (
    run_id, upload_id, validation_id, source_filename, sheet_name, input_digest,
    config_snapshot_json, prompt_snapshot_json, versions_json, status,
    total_count, valid_count, input_invalid_count, skipped_blank_rows, created_at
) VALUES (?, 'U1', 'V1', '样本.xlsx', 'Sheet1', 'digest',
          '{}', '{}', '{}', ?, ?, ?, ?, ?, '2026-10-06T00:00:00+00:00')
"""

RECORD_INSERT = """
INSERT INTO records (
    record_key, run_id, record_id, source_row, order_index, q, a,
    refs_json, raw_input_json, status, final_label, review_required,
    created_at, updated_at
) VALUES (?, 'R1', ?, ?, ?, ?, ?, '[]', '{}', ?, ?, ?,
          '2026-10-06T00:00:00+00:00', '2026-10-06T00:00:00+00:00')
"""

NOW = "2026-10-06T00:00:00+00:00"


def make_connection(tmp_path: Path) -> sqlite3.Connection:
    """迁移好的空库连接（S04-01 的常规起点）。"""
    return storage.open_database(tmp_path / "state.sqlite3")


def seed_run(connection: sqlite3.Connection, **overrides: object) -> None:
    """插入一条上传/预检/批次以满足外键，供 records 相关用例使用。"""
    with storage.write_transaction(connection) as conn:
        conn.execute(
            "INSERT INTO uploads (upload_id, original_filename, relative_path, sha256,"
            " size_bytes, sheets_json, created_at)"
            " VALUES ('U1', '样本.xlsx', 'uploads/U1.xlsx', 'a' || '0' * 63, 1024, '[]', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO input_validations (validation_id, upload_id, sheet_name, status,"
            " file_sha256, input_contract_version, counts_json, report_json, created_at)"
            " VALUES ('V1', 'U1', 'Sheet1', 'passed', 'a' || '0' * 63, '1', '{}', '{}', ?)",
            (NOW,),
        )
        values = {
            "status": "queued",
            "total_count": 1,
            "valid_count": 1,
            "input_invalid_count": 0,
            "skipped_blank_rows": 0,
        }
        values.update(overrides)
        conn.execute(
            RUN_INSERT,
            (
                "R1",
                values["status"],
                values["total_count"],
                values["valid_count"],
                values["input_invalid_count"],
                values["skipped_blank_rows"],
            ),
        )


def make_migrations_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 `discover_migrations()` 指向临时目录，便于构造多版本场景。"""
    directory = tmp_path / "migrations"
    directory.mkdir()
    monkeypatch.setattr(database, "MIGRATIONS_DIR", directory)
    return directory


def write_migration(directory: Path, version: int, name: str, sql: str) -> Path:
    path = directory / f"{version:03d}_{name}.sql"
    path.write_text(sql, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 连接配置


def test_connect_enables_foreign_keys_and_wal(tmp_path: Path) -> None:
    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # 自动提交模式：事务由 write_transaction 显式控制，不混用隐式事务。
        assert connection.isolation_level is None
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_connect_rejects_non_wal_target(tmp_path: Path) -> None:
    """内存库拿不到 WAL，必须当场报错而不是悄悄降级。"""
    with pytest.raises(storage.DatabaseError, match="WAL"):
        storage.connect(Path(":memory:"))


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "state.sqlite3"
    connection = storage.connect(target)
    connection.close()
    assert target.exists()


# ------------------------------------------------------------------ 短事务


def test_write_transaction_commits(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_write_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with pytest.raises(sqlite3.IntegrityError):
            with storage.write_transaction(connection) as conn:
                conn.execute("UPDATE runs SET status = 'running'")
                # 计数恒等式被破坏，CHECK 触发 → 整个事务回滚。
                conn.execute("UPDATE runs SET total_count = 99")
        assert connection.execute("SELECT status FROM runs").fetchone()["status"] == "queued"
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_write_transaction_rejects_nesting(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        with pytest.raises(storage.DatabaseError, match="不可嵌套"):
            with storage.write_transaction(connection):
                with storage.write_transaction(connection):
                    pass  # pragma: no cover - 不会执行到这里
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_execute_write_uses_own_transaction(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        with pytest.raises(storage.DatabaseError, match="不可嵌套"):
            with storage.write_transaction(connection):
                storage.execute_write(connection, "SELECT 1")
    finally:
        connection.close()


# ------------------------------------------------------------------ 结构版本与迁移


def test_current_version_of_fresh_database_is_zero(tmp_path: Path) -> None:
    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        assert storage.current_version(connection) == 0
    finally:
        connection.close()


def test_discover_migrations_reads_packaged_scripts() -> None:
    """随包分发的脚本按版本升序、无重复、摘要合法。

    版本号参照 `SCHEMA_VERSION` 而不是写死：后续每加一个迁移脚本都要改断言，
    会让「加了迁移但忘了抬版本」这件事在测试里悄悄通过。
    """
    migrations = storage.discover_migrations()
    assert [item.version for item in migrations] == list(
        range(1, storage.SCHEMA_VERSION + 1)
    )
    assert migrations[0].path.name == "001_init.sql"
    assert "CREATE TABLE runs" in migrations[0].sql
    assert all(len(item.sha256) == 64 for item in migrations)


def test_v2_migration_upgrades_an_existing_v1_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结构变更走**新增脚本**：旧库升到 v2，既有行保留、新列取默认值。"""
    path = tmp_path / "state.sqlite3"
    # 先把脚本目录限制到 v1：程序拒绝运行高于自己理解的脚本，因此不能直接用
    # 随包的 001+002 造出一个停在 v1 的旧库。
    packaged = database.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    shutil.copy(packaged / "001_init.sql", staged / "001_init.sql")
    monkeypatch.setattr(database, "MIGRATIONS_DIR", staged)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 1)
    connection = storage.connect(path)
    try:
        storage.migrate(connection)
        assert storage.current_version(connection) == 1
        assert "simulated" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(call_attempts)")
        }
        connection.execute(
            "INSERT INTO runtime_state (singleton_id, worker_id, updated_at)"
            " VALUES (1, 'old-worker', '2026-10-06T00:00:00+08:00')"
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(database, "MIGRATIONS_DIR", packaged)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 2)
    connection = storage.connect(path)
    try:
        result = storage.migrate(connection)
        assert result.applied_now == (2,)
        columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(call_attempts)")
        }
        # 默认 0（真实）：缺省不能把未知调用误标成模拟。
        assert columns["simulated"]["notnull"] == 1
        assert columns["simulated"]["dflt_value"] == "0"
        (row,) = connection.execute("SELECT * FROM runtime_state")
        assert row["worker_id"] == "old-worker"
        assert row["worker_mode"] is None
    finally:
        connection.close()


def test_migrate_creates_all_tables(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        assert storage.current_version(connection) == storage.SCHEMA_VERSION
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(EXPECTED_TABLES) <= names
        assert "schema_migrations" in names
    finally:
        connection.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        result = storage.migrate(connection)
        assert result.applied_now == ()
        assert result.version == storage.SCHEMA_VERSION
        assert result.backup_path is None
    finally:
        connection.close()


def test_open_database_migrates_and_returns_usable_connection(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        connection.close()


def test_migrate_backs_up_before_applying_new_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    # 本用例要造出「有待应用脚本」的情形，临时把程序理解的版本抬高一档。
    pending = database.SCHEMA_VERSION + 1
    monkeypatch.setattr(database, "SCHEMA_VERSION", pending)
    write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT PRIMARY KEY);")

    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        first = storage.migrate(connection, backup_dir=tmp_path / "backups")
        # 全新库没有可丢的内容，不产生备份。
        assert first.applied_now == (1,)
        assert first.backup_path is None

        write_migration(
            directory, pending, "more", "CREATE TABLE second (id TEXT PRIMARY KEY);"
        )
        second = storage.migrate(connection, backup_dir=tmp_path / "backups")

        assert second.applied_now == (pending,)
        assert second.version == pending
        assert second.backup_path is not None and second.backup_path.exists()
        # 备份内容必须是**迁移前**的状态：只到版本 1，且没有 second 表。
        backup = sqlite3.connect(str(second.backup_path))
        try:
            assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
            tables = {
                row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "first" in tables and "second" not in tables
        finally:
            backup.close()
    finally:
        connection.close()


def test_migrate_rejects_rewritten_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT PRIMARY KEY);")

    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        storage.migrate(connection)
        write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT, extra TEXT);")
        with pytest.raises(storage.MigrationError, match="已被改写"):
            storage.migrate(connection)
    finally:
        connection.close()


def test_migrate_rejects_missing_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 2)
    write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT PRIMARY KEY);")
    second = write_migration(directory, 2, "more", "CREATE TABLE second (id TEXT PRIMARY KEY);")

    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        storage.migrate(connection)
        second.unlink()
        with pytest.raises(storage.MigrationError, match="找不到"):
            storage.migrate(connection)
    finally:
        connection.close()


def test_migrate_rejects_future_database_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT PRIMARY KEY);")

    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        storage.migrate(connection)
        storage.execute_write(
            connection,
            "INSERT INTO schema_migrations (version, script_sha256, applied_at)"
            " VALUES (99, ?, ?)",
            ("0" * 64, NOW),
        )
        with pytest.raises(storage.SchemaVersionError, match="高于本程序支持"):
            storage.migrate(connection)
    finally:
        connection.close()


def test_migrate_rejects_future_script_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT PRIMARY KEY);")
    write_migration(directory, 99, "future", "CREATE TABLE later (id TEXT PRIMARY KEY);")

    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(storage.SchemaVersionError, match="程序版本过旧"):
            storage.migrate(connection)
        # 拒绝要发生在写入之前：一个脚本都不该被应用。
        assert storage.current_version(connection) == 0
    finally:
        connection.close()


def test_failed_migration_rolls_back_ddl_and_version_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 2)
    write_migration(directory, 1, "init", "CREATE TABLE first (id TEXT PRIMARY KEY);")
    write_migration(
        directory,
        2,
        "broken",
        "CREATE TABLE second (id TEXT PRIMARY KEY);\n"
        "INSERT INTO no_such_table (id) VALUES ('x');",
    )

    connection = storage.connect(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(storage.MigrationError, match="应用迁移 002_broken.sql 失败"):
            storage.migrate(connection)
        # 建表与版本登记必须一起回滚，否则库会卡在「表已存在、版本未记」的中间态。
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "second" not in tables
        assert storage.current_version(connection) == 1
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_discover_migrations_rejects_bad_names_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    (directory / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(storage.MigrationError, match="命名"):
        storage.discover_migrations()

    (directory / "init.sql").unlink()
    write_migration(directory, 1, "a", "SELECT 1;")
    write_migration(directory, 1, "b", "SELECT 1;")
    with pytest.raises(storage.MigrationError, match="版本重复"):
        storage.discover_migrations()


def test_discover_migrations_rejects_empty_or_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_migrations_dir(tmp_path, monkeypatch)
    with pytest.raises(storage.MigrationError, match="为空"):
        storage.discover_migrations()

    monkeypatch.setattr(database, "MIGRATIONS_DIR", tmp_path / "nope")
    with pytest.raises(storage.MigrationError, match="不存在"):
        storage.discover_migrations()


def test_backup_database_copies_committed_state(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        target = storage.backup_database(connection, tmp_path / "backups" / "copy.sqlite3")
        copy = sqlite3.connect(str(target))
        try:
            assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        finally:
            copy.close()
    finally:
        connection.close()


def test_utc_now_is_sortable_iso8601() -> None:
    value = storage.utc_now()
    assert value.endswith("+00:00")
    assert len(value) == len("2026-10-06T00:00:00+00:00")


# ------------------------------------------------------------------ 表约束


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with storage.write_transaction(connection) as conn:
                conn.execute(
                    RECORD_INSERT,
                    ("K1", None, 2, 1, "q", "a", "pending", None, None),
                )
    finally:
        connection.close()


def test_runs_count_identity_is_enforced(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute_write(connection, "UPDATE runs SET total_count = 5")
        # valid_count >= 1：整批输入失败不是合法批次。
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute_write(connection, "UPDATE runs SET valid_count = 0")
    finally:
        connection.close()


def test_records_label_and_status_constraints(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with storage.write_transaction(connection) as conn:
            conn.execute(
                RECORD_INSERT,
                ("K1", "1", 2, 1, "q", "a", "completed", "回答正确", 1),
            )

        cases = (
            # 未知标签
            ("K2", "2", 3, 2, "q", "a", "completed", "也许正确", 0),
            # completed 必须有标签与复核标记
            ("K3", "3", 4, 3, "q", "a", "completed", None, None),
            # 非 completed 不得带标签
            ("K4", "4", 5, 4, "q", "a", "pending", "回答正确", None),
            # 有效记录必须有 q/a
            ("K5", "5", 6, 5, None, "a", "pending", None, None),
            # 复核标记只能是 0/1
            ("K6", "6", 7, 6, "q", "a", "completed", "回答正确", 2),
        )
        for case in cases:
            with pytest.raises(sqlite3.IntegrityError):
                with storage.write_transaction(connection) as conn:
                    conn.execute(RECORD_INSERT, case)

        # 输入失败行允许缺 q/a，但仍需给出错误详情以外的合法状态。
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K7", "7", 8, 7, None, None, "input_invalid", None, None))
    finally:
        connection.close()


def test_record_order_index_is_zero_based(tmp_path: Path) -> None:
    """order_index 按已接受的 S02 实际合同从 0 开始（见 001_init.sql 该列注释）。"""
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K1", "1", 2, 0, "q", "a", "pending", None, None))
        assert connection.execute("SELECT order_index FROM records").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            with storage.write_transaction(connection) as conn:
                conn.execute(RECORD_INSERT, ("K2", "2", 3, -1, "q", "a", "pending", None, None))
    finally:
        connection.close()


def test_records_order_and_row_are_unique_per_run(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K1", "1", 2, 1, "q", "a", "pending", None, None))
        with pytest.raises(sqlite3.IntegrityError):
            with storage.write_transaction(connection) as conn:
                conn.execute(RECORD_INSERT, ("K2", "2", 2, 9, "q", "a", "pending", None, None))
        with pytest.raises(sqlite3.IntegrityError):
            with storage.write_transaction(connection) as conn:
                conn.execute(RECORD_INSERT, ("K3", "3", 9, 1, "q", "a", "pending", None, None))
    finally:
        connection.close()


def test_record_id_unique_but_null_tolerant(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        # 缺编号的失败行可以有多条
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K1", None, 2, 1, "q", "a", "pending", None, None))
            conn.execute(RECORD_INSERT, ("K2", None, 3, 2, "q", "a", "pending", None, None))
        # 同一业务编号在同一批次内不得重复
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K3", "1", 4, 3, "q", "a", "pending", None, None))
        with pytest.raises(sqlite3.IntegrityError):
            with storage.write_transaction(connection) as conn:
                conn.execute(RECORD_INSERT, ("K4", "1", 5, 4, "q", "a", "pending", None, None))
    finally:
        connection.close()


def test_only_one_active_classification_per_run(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)

        def insert_job(job_id: str, status: str, kind: str = "classify", mode: str = "initial") -> None:
            storage.execute_write(
                connection,
                "INSERT INTO jobs (job_id, run_id, kind, mode, payload_json, status, created_at)"
                " VALUES (?, 'R1', ?, ?, '{}', ?, ?)",
                (job_id, kind, mode, status, NOW),
            )

        insert_job("J1", "completed")
        insert_job("J2", "queued")
        with pytest.raises(sqlite3.IntegrityError):
            insert_job("J3", "running")

        # 历史任务不占活跃位；导出任务不受该索引约束。
        storage.execute_write(connection, "UPDATE jobs SET status = 'failed' WHERE job_id = 'J2'")
        insert_job("J4", "queued", kind="export", mode="automatic")
        insert_job("J5", "queued", kind="export", mode="manual")
    finally:
        connection.close()


def test_worker_slot_is_exclusive(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        storage.execute_write(
            connection,
            "INSERT INTO jobs (job_id, run_id, kind, mode, payload_json, status, worker_slot, created_at)"
            " VALUES ('J1', 'R1', 'classify', 'initial', '{}', 'running', 1, ?)",
            (NOW,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute_write(
                connection,
                "INSERT INTO jobs (job_id, run_id, kind, mode, payload_json, status, worker_slot, created_at)"
                " VALUES ('J2', 'R1', 'classify', 'resume', '{}', 'running', 1, ?)",
                (NOW,),
            )
    finally:
        connection.close()


def test_automatic_export_is_unique_per_revision(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        storage.execute_write(
            connection,
            "INSERT INTO jobs (job_id, run_id, kind, mode, payload_json, status, created_at)"
            " VALUES ('J1', 'R1', 'export', 'automatic', '{}', 'completed', ?)",
            (NOW,),
        )

        def insert_export(export_id: str, job_id: str, source: str, revision: int) -> None:
            storage.execute_write(
                connection,
                "INSERT INTO exports (export_id, job_id, run_id, source, scheduled_revision,"
                " created_at) VALUES (?, ?, 'R1', ?, ?, ?)",
                (export_id, job_id, source, revision, NOW),
            )

        insert_export("E1", "J1", "automatic", 1)
        with pytest.raises(sqlite3.IntegrityError):
            insert_export("E2", "J1", "automatic", 1)
    finally:
        connection.close()


def test_attempt_and_campaign_constraints(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K1", "1", 2, 1, "q", "a", "pending", None, None))
            conn.execute(
                "INSERT INTO stage_campaigns (campaign_id, record_key, stage, campaign_no,"
                " max_attempts, reason, created_at)"
                " VALUES ('C1', 'K1', 1, 1, 3, 'initial', ?)",
                (NOW,),
            )
        # 阶段只能是 1/2，重试上限受 max_attempts 限制
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute_write(
                connection,
                "INSERT INTO stage_campaigns (campaign_id, record_key, stage, campaign_no,"
                " max_attempts, reason, created_at)"
                " VALUES ('C2', 'K1', 3, 1, 3, 'initial', ?)",
                (NOW,),
            )
        with storage.write_transaction(connection) as conn:
            conn.execute(
                "INSERT INTO call_attempts (attempt_id, campaign_id, attempt_no, status, started_at)"
                " VALUES ('A1', 'C1', 1, 'running', ?)",
                (NOW,),
            )
        # 同一 campaign 内尝试序号不得重复；状态枚举受限
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute_write(
                connection,
                "INSERT INTO call_attempts (attempt_id, campaign_id, attempt_no, status, started_at)"
                " VALUES ('A2', 'C1', 1, 'running', ?)",
                (NOW,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute_write(
                connection,
                "INSERT INTO call_attempts (attempt_id, campaign_id, attempt_no, status, started_at)"
                " VALUES ('A3', 'C1', 2, 'timeout', ?)",
                (NOW,),
            )
    finally:
        connection.close()


def test_stage_result_is_unique_per_record_and_stage(tmp_path: Path) -> None:
    connection = make_connection(tmp_path)
    try:
        seed_run(connection)
        with storage.write_transaction(connection) as conn:
            conn.execute(RECORD_INSERT, ("K1", "1", 2, 1, "q", "a", "pending", None, None))
            conn.execute(
                "INSERT INTO stage_campaigns (campaign_id, record_key, stage, campaign_no,"
                " max_attempts, reason, created_at)"
                " VALUES ('C1', 'K1', 1, 1, 3, 'initial', ?), ('C2', 'K1', 1, 2, 3,"
                " 'explicit_retry', ?)",
                (NOW, NOW),
            )
            conn.execute(
                "INSERT INTO call_attempts (attempt_id, campaign_id, attempt_no, status, started_at)"
                " VALUES ('A1', 'C1', 1, 'succeeded', ?), ('A2', 'C2', 1, 'succeeded', ?)",
                (NOW, NOW),
            )

        def insert_result(result_id: str, attempt_id: str) -> None:
            storage.execute_write(
                connection,
                "INSERT INTO stage_results (stage_result_id, record_key, stage, attempt_id,"
                " result_json, schema_version, prompt_sha256, result_sha256, validated_at)"
                " VALUES (?, 'K1', 1, ?, '{}', '1', ?, ?, ?)",
                (result_id, attempt_id, "a" * 64, "b" * 64, NOW),
            )

        insert_result("S1", "A1")
        # 每记录每阶段只有一个已校验结果：显式重试不能覆盖原结果。
        with pytest.raises(sqlite3.IntegrityError):
            insert_result("S2", "A2")
    finally:
        connection.close()
