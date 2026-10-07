-- S04-01：初始数据库结构（结构版本 1）。
--
-- 依据 plan/09-数据库与持久化设计.md §3—§6。约束分两层：
--   1) 本文件里的 CHECK / UNIQUE / 部分唯一索引 / 外键是数据库层保障；
--   2) 「跨表业务条件」（如「已存在 stage2 结果」）由服务层在同一事务内验证。
-- 本文件不可改写：已应用的脚本按 sha256 校验，改动会被 migrate() 拒绝。
-- 后续结构变更一律新增 002_*.sql，不修改本文件。

-- ---------------------------------------------------------------- 上传与预检

CREATE TABLE uploads (
    upload_id        TEXT    PRIMARY KEY,
    original_filename TEXT   NOT NULL,
    relative_path    TEXT    NOT NULL UNIQUE,
    sha256           TEXT    NOT NULL,
    size_bytes       INTEGER NOT NULL CHECK (size_bytes >= 0),
    sheets_json      TEXT    NOT NULL,
    created_at       TEXT    NOT NULL
);

CREATE TABLE input_validations (
    validation_id          TEXT PRIMARY KEY,
    upload_id              TEXT NOT NULL REFERENCES uploads (upload_id),
    sheet_name             TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('passed', 'blocked')),
    file_sha256            TEXT NOT NULL,
    input_contract_version TEXT NOT NULL,
    counts_json            TEXT NOT NULL,
    report_json            TEXT NOT NULL,
    created_at             TEXT NOT NULL
);

CREATE INDEX idx_input_validations_upload ON input_validations (upload_id);

-- ---------------------------------------------------------------- 批次与记录

CREATE TABLE runs (
    run_id                TEXT    PRIMARY KEY,
    upload_id             TEXT    NOT NULL REFERENCES uploads (upload_id),
    validation_id         TEXT    NOT NULL REFERENCES input_validations (validation_id),
    source_filename       TEXT    NOT NULL,
    sheet_name            TEXT    NOT NULL,
    input_digest          TEXT    NOT NULL,
    config_snapshot_json  TEXT    NOT NULL,
    prompt_snapshot_json  TEXT    NOT NULL,
    versions_json         TEXT    NOT NULL,
    status                TEXT    NOT NULL CHECK (status IN (
                              'queued', 'running', 'completed',
                              'partial_failed', 'failed', 'interrupted')),
    revision              INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    total_count           INTEGER NOT NULL CHECK (total_count >= 0),
    valid_count           INTEGER NOT NULL CHECK (valid_count >= 1),
    input_invalid_count   INTEGER NOT NULL CHECK (input_invalid_count >= 0),
    skipped_blank_rows    INTEGER NOT NULL CHECK (skipped_blank_rows >= 0),
    last_error_json       TEXT,
    created_at            TEXT    NOT NULL,
    started_at            TEXT,
    finished_at           TEXT,
    -- 计数恒等式：没有它就无法判断「100 = 10 + 90」这类口径是否被破坏。
    CHECK (total_count = valid_count + input_invalid_count)
);

CREATE INDEX idx_runs_created_at ON runs (created_at, run_id);
CREATE INDEX idx_runs_status ON runs (status, created_at);

CREATE TABLE records (
    record_key        TEXT    PRIMARY KEY,
    run_id            TEXT    NOT NULL REFERENCES runs (run_id),
    record_id         TEXT,
    source_row        INTEGER NOT NULL CHECK (source_row >= 1),
    -- 从 0 开始：与已接受的 S02 实现一致（schemas/qa.py 的 Field(ge=0)、
    -- excel/reader.py 的 order_index=len(records)）。plan/09 §46 写的是「从 1 开始」，
    -- 属规划文字与实际合同的分歧，已记入 S04 阶段文档 §0.4 ⑤ 待用户确认；
    -- 此处按**实际合同**建表，不在存储层做静默 +1 平移。
    order_index       INTEGER NOT NULL CHECK (order_index >= 0),
    q                 TEXT,
    a                 TEXT,
    refs_json         TEXT    NOT NULL,
    raw_input_json    TEXT    NOT NULL,
    input_error_json  TEXT,
    status            TEXT    NOT NULL CHECK (status IN (
                          'pending', 'input_invalid', 'stage1_done',
                          'completed', 'failed')),
    final_label       TEXT,
    review_required   INTEGER,
    failure_stage     TEXT CHECK (failure_stage IN ('stage1', 'stage2')),
    failure_json      TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    -- 顺序与来源行在批次内唯一：恢复与编号核对都依赖它。
    UNIQUE (run_id, order_index),
    UNIQUE (run_id, source_row),
    -- 三分类枚举与「非 completed 不得有标签/复核标记」。
    CHECK (final_label IS NULL OR final_label IN (
               '回答正确', '未检索到正确资料', '检索到正确资料但回答错误')),
    CHECK (review_required IS NULL OR review_required IN (0, 1)),
    CHECK (status = 'completed'
           OR (final_label IS NULL AND review_required IS NULL)),
    CHECK (status <> 'completed'
           OR (final_label IS NOT NULL AND review_required IS NOT NULL)),
    -- q/a 只在输入失败行允许为空；有效记录必须有提问与回答。
    CHECK (status = 'input_invalid' OR (q IS NOT NULL AND a IS NOT NULL))
);

CREATE INDEX idx_records_run_order ON records (run_id, order_index);
CREATE INDEX idx_records_run_status ON records (run_id, status);
CREATE INDEX idx_records_run_label ON records (run_id, final_label, order_index);
CREATE INDEX idx_records_run_review ON records (run_id, review_required, order_index);

-- 业务编号在批次内唯一；**空编号互不冲突**（缺编号的失败行仍可追踪）。
CREATE UNIQUE INDEX uq_run_record_id
    ON records (run_id, record_id)
    WHERE record_id IS NOT NULL;

-- ---------------------------------------------------------------- 任务与队列

CREATE TABLE jobs (
    job_id            TEXT    PRIMARY KEY,
    run_id            TEXT    NOT NULL REFERENCES runs (run_id),
    kind              TEXT    NOT NULL CHECK (kind IN ('classify', 'export')),
    mode              TEXT    NOT NULL,
    payload_json      TEXT    NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN (
                          'queued', 'running', 'completed',
                          'partial_failed', 'failed', 'interrupted')),
    worker_id         TEXT,
    worker_slot       INTEGER,
    current_record_key TEXT,
    current_stage     TEXT,
    result_json       TEXT,
    error_json        TEXT,
    created_at        TEXT    NOT NULL,
    started_at        TEXT,
    last_activity_at  TEXT,
    finished_at       TEXT,
    CHECK (worker_slot IS NULL OR worker_slot = 1),
    CHECK (kind <> 'classify' OR mode IN ('initial', 'resume', 'retry_failed')),
    CHECK (kind <> 'export' OR mode IN ('automatic', 'manual'))
);

CREATE INDEX idx_jobs_queue ON jobs (status, created_at, job_id);
CREATE INDEX idx_jobs_run ON jobs (run_id, created_at);

-- 执行槽：同一时刻最多一个 running 任务。
CREATE UNIQUE INDEX uq_worker_slot ON jobs (worker_slot) WHERE worker_slot IS NOT NULL;

-- 一个批次最多一个活跃判别任务（queued 或 running）。
CREATE UNIQUE INDEX uq_active_classification
    ON jobs (run_id)
    WHERE kind = 'classify' AND status IN ('queued', 'running');

-- ---------------------------------------------------------------- 预算与调用

CREATE TABLE stage_campaigns (
    campaign_id       TEXT    PRIMARY KEY,
    record_key        TEXT    NOT NULL REFERENCES records (record_key),
    stage             INTEGER NOT NULL CHECK (stage IN (1, 2)),
    campaign_no       INTEGER NOT NULL CHECK (campaign_no >= 1),
    max_attempts      INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 3),
    reason            TEXT    NOT NULL CHECK (reason IN ('initial', 'explicit_retry')),
    created_by_job_id TEXT    REFERENCES jobs (job_id),
    created_at        TEXT    NOT NULL,
    UNIQUE (record_key, stage, campaign_no)
);

CREATE INDEX idx_stage_campaigns_record ON stage_campaigns (record_key, stage, campaign_no);

CREATE TABLE call_attempts (
    attempt_id          TEXT    PRIMARY KEY,
    campaign_id         TEXT    NOT NULL REFERENCES stage_campaigns (campaign_id),
    job_id              TEXT    REFERENCES jobs (job_id),
    attempt_no          INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
    status              TEXT    NOT NULL CHECK (status IN (
                            'running', 'succeeded', 'failed', 'unknown_after_interrupt')),
    requested_model_id  TEXT,
    returned_model_id   TEXT,
    request_digest      TEXT,
    -- 最终响应正文（不是推理链）。是否经 API 暴露由 S06 定；此处按 plan/09 §4 落库。
    final_content       TEXT,
    usage_json          TEXT,
    duration_ms         INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    service_request_id  TEXT,
    error_json          TEXT,
    started_at          TEXT    NOT NULL,
    finished_at         TEXT,
    UNIQUE (campaign_id, attempt_no)
);

CREATE INDEX idx_call_attempts_campaign ON call_attempts (campaign_id, attempt_no);
CREATE INDEX idx_call_attempts_status ON call_attempts (status, started_at);
CREATE INDEX idx_call_attempts_job ON call_attempts (job_id);

CREATE TABLE stage_results (
    stage_result_id TEXT PRIMARY KEY,
    record_key      TEXT NOT NULL REFERENCES records (record_key),
    stage           INTEGER NOT NULL CHECK (stage IN (1, 2)),
    attempt_id      TEXT NOT NULL REFERENCES call_attempts (attempt_id),
    result_json     TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    prompt_sha256   TEXT NOT NULL,
    result_sha256   TEXT NOT NULL,
    validated_at    TEXT NOT NULL,
    -- 每记录每阶段只有一个已校验结果；一个成功调用只对应一个结果。
    UNIQUE (record_key, stage),
    UNIQUE (attempt_id)
);

-- ---------------------------------------------------------------- 导出与幂等

CREATE TABLE exports (
    export_id           TEXT    PRIMARY KEY,
    job_id              TEXT    NOT NULL UNIQUE REFERENCES jobs (job_id),
    run_id              TEXT    NOT NULL REFERENCES runs (run_id),
    source              TEXT    NOT NULL CHECK (source IN ('automatic', 'manual')),
    scheduled_revision  INTEGER NOT NULL CHECK (scheduled_revision >= 0),
    captured_revision   INTEGER,
    captured_at         TEXT,
    captured_run_status TEXT,
    captured_counts_json TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX idx_exports_run ON exports (run_id, created_at);

-- 同一批次在同一 revision 上只自动安排一次导出（手动导出不受限）。
CREATE UNIQUE INDEX uq_automatic_export_revision
    ON exports (run_id, scheduled_revision)
    WHERE source = 'automatic';

CREATE TABLE artifacts (
    artifact_id   TEXT    PRIMARY KEY,
    export_id     TEXT    NOT NULL REFERENCES exports (export_id),
    kind          TEXT    NOT NULL CHECK (kind IN ('excel', 'jsonl')),
    relative_path TEXT    NOT NULL UNIQUE,
    download_name TEXT    NOT NULL,
    media_type    TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    -- 两份完整文件共同发布：同一导出里每种文件只能有一个。
    UNIQUE (export_id, kind)
);

CREATE TABLE idempotency_keys (
    scope             TEXT    NOT NULL,
    key               TEXT    NOT NULL,
    request_sha256    TEXT    NOT NULL,
    http_status       INTEGER NOT NULL,
    response_data_json TEXT   NOT NULL,
    run_id            TEXT    REFERENCES runs (run_id),
    job_id            TEXT    REFERENCES jobs (job_id),
    created_at        TEXT    NOT NULL,
    PRIMARY KEY (scope, key)
);

-- ---------------------------------------------------------------- 运行时状态

CREATE TABLE runtime_state (
    singleton_id         INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    worker_id            TEXT,
    worker_started_at    TEXT,
    model_dispatch_paused INTEGER NOT NULL DEFAULT 0
                         CHECK (model_dispatch_paused IN (0, 1)),
    pause_reason_json    TEXT,
    updated_at           TEXT    NOT NULL
);
