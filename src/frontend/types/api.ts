// S03-07：前端使用的接口类型，字段与 plan/08 及后端返回保持一致。
// 标签、状态与证据一律按后端返回的文本展示，前端不推导业务类别。

export type JudgeMode = 'mock' | 'real';
export type PrecheckStatus = 'passed' | 'blocked';
export type JobStatus = 'queued' | 'running' | 'completed' | 'partial_failed' | 'failed';

export interface SheetInfo {
  name: string;
  visible: boolean;
}

/** 可下载的合成样例；仓库不提交二进制 .xlsx，样例由后端现场生成。 */
export interface SampleInfo {
  name: string;
  filename: string;
  description: string;
  record_count: number;
}

export interface UploadData {
  upload_id: string;
  original_filename: string;
  size_bytes: number;
  sha256: string;
  sheets: SheetInfo[];
  suggested_sheet: string | null;
}

export interface Blocker {
  code: string;
  message: string;
  field?: string | null;
  source_rows?: number[];
}

export interface RowError {
  source_row: number;
  record_id?: string | null;
  reason: string;
}

export interface PrecheckWarning {
  code: string;
  message: string;
}

export interface PrecheckCounts {
  total: number | null;
  valid: number | null;
  input_invalid: number | null;
  skipped_blank_rows: number | null;
}

export interface RecordSummary {
  record_key: string;
  record_id?: string | null;
  source_row: number;
  order_index: number;
  q_preview?: string | null;
  ref_count: number;
}

export interface ValidationData {
  validation_id: string;
  upload_id: string;
  status: PrecheckStatus;
  sheet_name: string | null;
  input_contract_version: string;
  file_sha256: string;
  counts: PrecheckCounts;
  blockers: Blocker[];
  row_errors: RowError[];
  warnings: PrecheckWarning[];
  records: RecordSummary[];
}

export interface Evidence {
  ref_id: string;
  quote: string;
}

export interface Stage1Analysis {
  required_points: string[];
  evidence_sufficiency: 'sufficient' | 'insufficient' | 'uncertain';
  evidence: Evidence[];
  missing_information: string[];
  conflicts: string[];
  reason: string;
}

export interface Stage1Correction {
  corrected_item: string;
  reason: string;
  evidence: Evidence[];
  affects_classification: boolean;
}

export interface Stage2Judgement {
  label: string;
  reason: string;
  issues: string[];
  evidence_sufficiency: 'sufficient' | 'insufficient' | 'uncertain';
  evidence: Evidence[];
  review_required: boolean;
  review_reasons: string[];
  stage1_corrections: Stage1Correction[];
}

export interface AttemptSummary {
  stage: string;
  attempt: number;
  outcome: 'ok' | 'validation_error' | 'model_error';
  model: string | null;
  latency_ms: number | null;
  simulated: boolean;
  usage: {
    prompt_tokens: number | null;
    completion_tokens: number | null;
    total_tokens: number | null;
  } | null;
  error_code: string | null;
  error_message: string | null;
  /** 仅校验失败的尝试非空：被拒的模型输出正文（非推理链），供人工判读失败原因。 */
  raw_output: string | null;
  raw_output_truncated: boolean;
}

export interface RecordFailure {
  stage: string;
  code: string;
  message: string;
  attempt_count: number;
  retryable: boolean;
}

export interface RecordResultPayload {
  record_id: string | null;
  source_row: number;
  status: 'completed' | 'failed';
  label: string | null;
  simulated: boolean;
  stage1: Stage1Analysis | null;
  stage2: Stage2Judgement | null;
  failure: RecordFailure | null;
  attempts: AttemptSummary[];
}

/** POST /api/judge 的返回：任务已提交，结果需轮询 JobPayload。 */
export interface JudgeCreated {
  job_id: string;
  mode: JudgeMode;
  status: JobStatus;
}

export interface JobPayload {
  job_id: string;
  kind: string;
  mode: JudgeMode;
  status: JobStatus;
  current_stage: string | null;
  /** 本阶段正在进行的第几次尝试；终态为 null。 */
  current_attempt: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** 已用毫秒；运行中由轮询刷新，终态为最终值。可空（尚未开始）。 */
  elapsed_ms: number | null;
  validation_id: string;
  record_key: string;
  result: RecordResultPayload | null;
  error: { code: string; message: string } | null;
}
