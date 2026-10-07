"""S04-02 上传/预检/批次/记录持久化的单测。

覆盖：批次创建一次提交（runs + records + jobs + 幂等键同事务）、计数与状态映射、
输入失败行落库、`record_key` 是内部 UUID 而非业务编号、快照不含凭据、
幂等键的三条规则（同键同体复用、同键异体冲突、失败不留记录）、
以及「原文件变了 / 解析实现变了」两种拒绝路径。

全部使用合成工作簿与本地 SQLite，**零真实模型调用**。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from aidhu_om_agent.config import (
    AppConfig,
    ExecutionConfig,
    LimitsConfig,
    ModelConfig,
    PathsConfig,
)
from aidhu_om_agent.excel.reader import precheck
from aidhu_om_agent.prompts import load_prompt
from aidhu_om_agent.repositories import jobs as jobs_repo
from aidhu_om_agent.repositories import records as records_repo
from aidhu_om_agent.repositories import runs as runs_repo
from aidhu_om_agent.services.batches import (
    RUN_CREATE_SCOPE,
    BatchError,
    create_run,
    load_parsed_input,
)
from aidhu_om_agent.services.uploads import UploadStore
from aidhu_om_agent.storage import SCHEMA_VERSION, Database, open_database
from aidhu_om_agent.version import prompt_fingerprint
from fixtures.excel_samples import (
    all_invalid_workbook,
    normal_workbook,
    partial_failure_workbook,
)

SECRET = "sk-do-not-store-this-value"
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def make_config(tmp_path: Path) -> AppConfig:
    """凭据非空，用于证明快照不落密钥。"""
    model = ModelConfig(
        base_url="https://example.invalid/v1",
        model="test-model",
        timeout_seconds=1.0,
        api_key=SECRET,
    )
    return AppConfig(
        stage1=model,
        stage2=model,
        execution=ExecutionConfig(
            concurrency=1, max_attempts_per_stage_campaign=3, retry_backoff_seconds=(0, 0)
        ),
        limits=LimitsConfig(max_upload_bytes=4 * 1024 * 1024, max_records=1000),
        paths=PathsConfig(
            database=tmp_path / "runtime" / "state.sqlite3",
            uploads=tmp_path / "runtime" / "uploads",
            runtime=tmp_path / "runtime",
            outputs=tmp_path / "outputs",
            logs=tmp_path / "logs",
            frontend_dist=tmp_path / "dist",
        ),
        source_config=tmp_path / "config.toml",
        source_env=None,
    )


def prepare_validation(
    tmp_path: Path, workbook: Path, config: AppConfig
) -> tuple[Database, str]:
    """把工作簿按真实路径走一遍「上传 → 预检 → 落库」，返回 (库, validation_id)。"""
    database = Database(config.paths.database)
    database.initialize()
    store = UploadStore(database, config.paths.uploads)
    with workbook.open("rb") as handle:
        upload = store.save(workbook.name, handle, limits=config.limits)
    parsed = precheck(upload.path, None, limits=config.limits)
    validation = store.put_validation(upload.upload_id, parsed)
    return database, validation.validation_id


def read_rows(database: Database, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    connection = database.connect()
    try:
        return list(connection.execute(sql, parameters))
    finally:
        connection.close()


# ------------------------------------------------------------------ 创建批次


def test_create_run_persists_batch_records_and_job(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = partial_failure_workbook(tmp_path / "src" / "partial.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)

    created = create_run(database, config, validation_id=validation_id)

    assert HEX32.match(created.run_id) and HEX32.match(created.job_id)
    assert created.reused is False and created.http_status == 202

    (run,) = read_rows(database, "SELECT * FROM runs")
    assert run["status"] == "queued"
    assert run["revision"] == 0
    # 「编号/问题/回答」各缺一条 + 1 条有效；空行另计不进 total。
    assert (run["total_count"], run["valid_count"]) == (4, 1)
    assert (run["input_invalid_count"], run["skipped_blank_rows"]) == (3, 2)
    assert run["validation_id"] == validation_id
    assert run["started_at"] is None and run["finished_at"] is None

    rows = read_rows(database, "SELECT * FROM records ORDER BY order_index")
    assert [row["order_index"] for row in rows] == [0, 1, 2, 3]
    assert [row["status"] for row in rows] == [
        "pending",
        "input_invalid",
        "input_invalid",
        "input_invalid",
    ]
    assert all(HEX32.match(row["record_key"]) for row in rows)
    # 业务编号缺失的那一行照样有内部主键，两者不是一回事。
    assert rows[0]["record_id"] == "1"
    assert rows[1]["record_id"] is None
    assert len({row["record_key"] for row in rows}) == 4

    (job,) = read_rows(database, "SELECT * FROM jobs")
    assert (job["kind"], job["mode"], job["status"]) == ("classify", "initial", "queued")
    assert job["worker_slot"] is None
    assert json.loads(job["payload_json"])["record_keys"] == [
        row["record_key"] for row in rows
    ]


def test_records_round_trip_through_new_connection(tmp_path: Path) -> None:
    """服务重启后仍能读出同一批记录（S04 的完成条件之一）。"""
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(database, config, validation_id=validation_id)

    reopened = open_database(config.paths.database)
    try:
        rows = records_repo.list_records(reopened, created.run_id)
        assert len(rows) == 3
        first = rows[0]
        assert first.status == "pending"
        assert first.to_qa_record().q is not None
        # 空资料还原成 None（而不是空字符串），提示词按 None 判空。
        assert isinstance(first.refs, dict)
        assert set(first.refs) == {f"ref{i}" for i in range(1, 11)}
        assert list(first.raw_input) == [
            "编号",
            "q",
            "a",
            *[f"ref{i}" for i in range(1, 11)],
        ]
        assert records_repo.count_by_status(reopened, created.run_id) == {
            "pending": 3,
            "input_invalid": 0,
            "stage1_done": 0,
            "completed": 0,
            "failed": 0,
        }
    finally:
        reopened.close()


def test_run_snapshots_are_frozen_and_credential_free(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(database, config, validation_id=validation_id)

    (run,) = read_rows(database, "SELECT * FROM runs")
    for column in ("config_snapshot_json", "versions_json", "prompt_snapshot_json"):
        assert SECRET not in run[column]

    snapshot = json.loads(run["config_snapshot_json"])
    assert snapshot["stage1"]["api_key_present"] is True
    assert "api_key" not in snapshot["stage1"]

    versions = json.loads(run["versions_json"])
    # 提示词版本随 S04-08 升到 1.2（只改提示词）；输入契约与 schema 不变。
    assert versions["stage1_prompt"] == "1.2" and versions["input_contract"] == "1.0"
    assert versions["schema"] == SCHEMA_VERSION

    prompts = json.loads(run["prompt_snapshot_json"])
    assert prompts["stage1"]["text"] == load_prompt("stage1")
    assert prompts["stage1"]["sha256"] == prompt_fingerprint(load_prompt("stage1"))
    assert prompts["stage2"]["sha256"] == prompt_fingerprint(load_prompt("stage2"))

    # 恢复必须走快照（而不是当前磁盘上的提示词）：读口单独核对一次。
    connection = database.connect()
    try:
        assert runs_repo.get_prompt_snapshot(connection, created.run_id) == prompts
    finally:
        connection.close()


def test_two_runs_from_same_validation_are_allowed(tmp_path: Path) -> None:
    """有意重跑：新幂等键可以再建一个批次（plan/08 §4 不禁止有意重新运行）。"""
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)

    first = create_run(database, config, validation_id=validation_id)
    second = create_run(database, config, validation_id=validation_id)

    assert first.run_id != second.run_id
    assert len(read_rows(database, "SELECT * FROM runs")) == 2
    assert len(read_rows(database, "SELECT * FROM jobs")) == 2
    assert len(read_rows(database, "SELECT * FROM records")) == 6


# ------------------------------------------------------------------ 拒绝路径


def test_create_run_rejects_blocked_validation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = all_invalid_workbook(tmp_path / "src" / "invalid.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)

    with pytest.raises(BatchError) as error:
        create_run(database, config, validation_id=validation_id)

    assert error.value.code == "INPUT_NOT_VALIDATED"
    assert error.value.http_status == 409
    assert read_rows(database, "SELECT * FROM runs") == []


def test_create_run_rejects_unknown_validation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database = Database(config.paths.database)
    database.initialize()

    with pytest.raises(BatchError) as error:
        create_run(database, config, validation_id="0" * 32)

    assert (error.value.code, error.value.http_status) == ("NOT_FOUND", 404)


def test_create_run_rejects_changed_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)

    # 预检之后**服务端保存的那份文件**被改写：文件摘要对不上，拒绝用旧 validation_id。
    stored = next(config.paths.uploads.iterdir())
    stored.write_bytes(stored.read_bytes() + b"tampered")

    with pytest.raises(BatchError) as error:
        create_run(database, config, validation_id=validation_id)

    assert error.value.code == "INPUT_SNAPSHOT_CHANGED"
    assert read_rows(database, "SELECT * FROM runs") == []


def test_parsed_input_must_match_persisted_report(tmp_path: Path) -> None:
    """S04 阶段文档 §0.4 ① 的核对：重算结果与快照不一致即拒绝。"""
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)

    connection = database.connect()
    try:
        parsed = load_parsed_input(connection, validation_id, config=config)
        assert parsed.report.counts.valid == 3

        stored = json.loads(
            connection.execute(
                "SELECT report_json FROM input_validations WHERE validation_id = ?",
                (validation_id,),
            ).fetchone()["report_json"]
        )
        stored["counts"]["valid"] = 99
        connection.execute(
            "UPDATE input_validations SET report_json = ? WHERE validation_id = ?",
            (json.dumps(stored, ensure_ascii=False), validation_id),
        )
        connection.commit()

        with pytest.raises(BatchError) as error:
            load_parsed_input(connection, validation_id, config=config)
        assert error.value.code == "VERSION_INCOMPATIBLE"
    finally:
        connection.close()


# ------------------------------------------------------------------ 幂等键


def test_same_key_returns_original_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)

    first = create_run(
        database, config, validation_id=validation_id, idempotency_key="k1", request_sha256="d1"
    )
    again = create_run(
        database, config, validation_id=validation_id, idempotency_key="k1", request_sha256="d1"
    )

    assert again.reused is True
    assert (again.run_id, again.job_id) == (first.run_id, first.job_id)
    assert len(read_rows(database, "SELECT * FROM runs")) == 1
    assert len(read_rows(database, "SELECT * FROM jobs")) == 1


def test_same_key_different_body_conflicts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    create_run(
        database, config, validation_id=validation_id, idempotency_key="k1", request_sha256="d1"
    )

    with pytest.raises(BatchError) as error:
        create_run(
            database,
            config,
            validation_id=validation_id,
            idempotency_key="k1",
            request_sha256="d2",
        )

    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(read_rows(database, "SELECT * FROM runs")) == 1


def test_failed_creation_leaves_no_idempotency_record(tmp_path: Path) -> None:
    """失败操作不留幂等记录：修正后可以用同一个键重试（plan/08 §4）。"""
    config = make_config(tmp_path)
    database = Database(config.paths.database)
    database.initialize()

    with pytest.raises(BatchError):
        create_run(
            database,
            config,
            validation_id="0" * 32,
            idempotency_key="k1",
            request_sha256="d1",
        )
    assert read_rows(database, "SELECT * FROM idempotency_keys") == []

    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    _, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(
        database, config, validation_id=validation_id, idempotency_key="k1", request_sha256="d1"
    )
    assert created.reused is False


def test_idempotency_scope_is_recorded(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(
        database, config, validation_id=validation_id, idempotency_key="k1", request_sha256="d1"
    )

    (row,) = read_rows(database, "SELECT * FROM idempotency_keys")
    assert row["scope"] == RUN_CREATE_SCOPE
    assert row["http_status"] == 202
    assert row["run_id"] == created.run_id
    assert json.loads(row["response_data_json"]) == {
        "run_id": created.run_id,
        "job_id": created.job_id,
    }

    connection = database.connect()
    try:
        stored = jobs_repo.get_idempotent(connection, scope=RUN_CREATE_SCOPE, key="k1")
        assert stored is not None
        assert stored.http_status == 202 and stored.run_id == created.run_id
        assert stored.response_data == {"run_id": created.run_id, "job_id": created.job_id}
        assert jobs_repo.get_idempotent(connection, scope=RUN_CREATE_SCOPE, key="nope") is None
    finally:
        connection.close()


def test_job_lookup_by_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    workbook = normal_workbook(tmp_path / "src" / "normal.xlsx")
    database, validation_id = prepare_validation(tmp_path, workbook, config)
    created = create_run(database, config, validation_id=validation_id)

    connection = database.connect()
    try:
        job = jobs_repo.get_job(connection, created.job_id)
        assert job is not None and job.run_id == created.run_id
        assert jobs_repo.find_active_classification(connection, created.run_id) is not None
        assert len(jobs_repo.list_jobs_for_run(connection, created.run_id)) == 1
    finally:
        connection.close()
