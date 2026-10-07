-- S07-03：评估结果落库（结构版本 3）。
--
-- 为什么要有这两张表：评估**不改动**任何既有数据（不写标签、不动预测、不碰导出），
-- 它是一份「在某次批次结果上按人工基准算出来的对照结论」。因此评估必须能重复执行
-- 并各自留痕——同一批次跑过校准集与保留集、或同一 split 在不同版本下跑过几次，
-- 都是不同的事实，不能互相覆盖。
--
-- 口径（S07 阶段文档 §0.3）：
--   1) 计分只用 `records.final_label`（agent 原预测）；人工修订不计作预测。
--   2) 逐条对照与指标一起落库，页面读库即可复原当时的数字，不必重算。
--   3) `split_json` 保存划分清单的关键内容（种子、逐类计数、被排除编号与理由），
--      清单文件被移动或删除后，评估记录仍然自洽可读。
--
-- 本文件不可改写：已应用的脚本按 sha256 校验，后续结构变更新增 004_*.sql。

CREATE TABLE evaluations (
    evaluation_id         TEXT    PRIMARY KEY,
    run_id                TEXT    NOT NULL REFERENCES runs (run_id),
    -- 本次评估用的是哪一部分：校准集用于调规则，保留集用于正式验收。
    split                 TEXT    NOT NULL CHECK (split IN ('calibration', 'holdout')),
    gold_filename         TEXT    NOT NULL,
    gold_sha256           TEXT    NOT NULL,
    gold_data_version     TEXT    NOT NULL,
    gold_contract_version TEXT    NOT NULL,
    manifest_filename     TEXT    NOT NULL,
    manifest_sha256       TEXT    NOT NULL,
    manifest_version      TEXT    NOT NULL,
    seed                  INTEGER NOT NULL,
    -- 划分清单要点：种子、逐类计数、被排除编号与理由、强制入校准集的编号。
    split_json            TEXT    NOT NULL,
    -- 批次执行时的版本快照（提示词、schema、模型配置），即「这批预测是哪个版本跑的」。
    versions_json         TEXT    NOT NULL,
    metrics_json          TEXT    NOT NULL,
    thresholds_json       TEXT    NOT NULL,
    report_json_name      TEXT    NOT NULL,
    report_text_name      TEXT    NOT NULL,
    valid_count           INTEGER NOT NULL CHECK (valid_count >= 0),
    scored_count          INTEGER NOT NULL CHECK (scored_count >= 0),
    excluded_count        INTEGER NOT NULL CHECK (excluded_count >= 0),
    created_at            TEXT    NOT NULL,
    -- 完成率的分母是有效记录数、分子是已分类数：这两个数不能互相矛盾。
    CHECK (scored_count <= valid_count)
);

CREATE INDEX idx_evaluations_run ON evaluations (run_id, created_at, evaluation_id);
CREATE INDEX idx_evaluations_split ON evaluations (run_id, split, created_at);

CREATE TABLE evaluation_records (
    evaluation_id   TEXT    NOT NULL REFERENCES evaluations (evaluation_id),
    record_id       TEXT    NOT NULL,
    -- 批次里对应的记录；用于从评估逐条跳到记录详情页。
    record_key      TEXT    REFERENCES records (record_key),
    gold_label      TEXT    NOT NULL CHECK (gold_label IN (
                        '回答正确', '未检索到正确资料', '检索到正确资料但回答错误')),
    -- NULL 表示该条没有合法预测：技术失败或未跑完（由 status/failure_stage 区分）。
    agent_label     TEXT    CHECK (agent_label IS NULL OR agent_label IN (
                        '回答正确', '未检索到正确资料', '检索到正确资料但回答错误')),
    agree           INTEGER CHECK (agree IS NULL OR agree IN (0, 1)),
    review_required INTEGER CHECK (review_required IS NULL OR review_required IN (0, 1)),
    record_status   TEXT    NOT NULL,
    failure_stage   TEXT    CHECK (failure_stage IS NULL OR failure_stage IN ('stage1', 'stage2')),
    -- 一致与否只能在没有预测之外的情形下判定；有预测就必须给出一致性。
    CHECK ((agent_label IS NULL) = (agree IS NULL)),
    PRIMARY KEY (evaluation_id, record_id)
);

CREATE INDEX idx_evaluation_records_agree
    ON evaluation_records (evaluation_id, agree, record_id);
CREATE INDEX idx_evaluation_records_key
    ON evaluation_records (evaluation_id, record_key);
