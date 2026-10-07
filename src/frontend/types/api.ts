// S03-07：前端使用的接口类型，字段与 plan/08 及后端返回保持一致。
// 标签、状态与证据一律按后端返回的文本展示，前端不推导业务类别。

export type PrecheckStatus = 'passed' | 'blocked';
// 任务与批次共用同一套状态（plan/10 §2）；`interrupted` 只在进程被停掉后由新 worker
// 的启动恢复写入，`/api/judge` 的临时内存任务不会产生它。
export type JobStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'partial_failed'
  | 'failed'
  | 'interrupted';

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

// S06-05 删除：`RecordResultPayload` 与 `JudgeCreated` 是 `POST /api/judge` 的返回形状。
// 该接口已删除，单条结果改由记录详情（`RunRecordDetail`）承载。

// ------------------------------------------------- S04-07：批次列表与详情

/** 批次详情计数；口径见 plan/08 §5，`review_required` 是 `classified` 的子集。 */
export interface RunCounts {
  total: number;
  valid: number;
  input_invalid: number;
  classified: number;
  failed: number;
  remaining: number;
  processed: number;
  review_required: number;
}

/** 列表项：批次标识、来源文件名、状态、计数、创建时间。 */
export interface RunListItem {
  run_id: string;
  original_filename: string;
  sheet_name: string;
  status: JobStatus;
  revision: number;
  counts: RunCounts;
  progress_percent: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunListPage {
  items: RunListItem[];
  page: number;
  page_size: number;
  total: number;
}

/** 任务摘要；不含 payload（里面是整批记录键）。 */
export interface RunJobSummary {
  job_id: string;
  kind: string;
  mode: string;
  status: JobStatus;
  current_record_key: string | null;
  current_stage: string | null;
  worker_id: string | null;
  created_at: string;
  started_at: string | null;
  last_activity_at: string | null;
  finished_at: string | null;
  error: { code: string; message: string } | null;
  result: { counts?: Record<string, number>; attempted?: number } | null;
}

/** 模型消费闸门与最近一次 worker 归属；**不据时间戳判定进程是否存活**。 */
export interface RunExecutionControl {
  model_dispatch_paused: boolean;
  pause_reason: { code?: string; message?: string; job_id?: string } | null;
  last_worker: { worker_id: string; started_at: string | null; mode: string | null } | null;
  runtime_updated_at: string | null;
}

/** `allowed_actions`：按钮的可用性与原因由后端给出，前端不自行判断。 */
export interface RunAllowedActions {
  can_resume: boolean;
  can_retry_failed: boolean;
  finalization_required: boolean;
  selected_records: number;
  renewed_campaigns: number;
  skipped_budget_exhausted: number;
  skipped_needs_new_batch: number;
  retry_failed_selected?: number;
  retry_failed_renewed?: number;
  remaining_rows?: number;
  disabled_reason: string | null;
  model_dispatch_paused: boolean;
  /** 导出按钮；`export_disabled_reason` 为真时给出后端自己的原因。 */
  can_export: boolean;
  export_disabled_reason: string | null;
}

/** 失败记录摘要：编号 + 失败阶段 + 错误码；**不含证据正文**。 */
export interface RunFailureItem {
  record_key: string;
  record_id: string | null;
  source_row: number;
  order_index: number;
  failure_stage: string | null;
  code: string | null;
  message: string | null;
  retryable: boolean | null;
  attempt_count: number | null;
}

export interface RunFailureSummary {
  count: number;
  items: RunFailureItem[];
  truncated: boolean;
}

/** 按阶段统计尝试；用于核对恢复没有重复调用阶段一。 */
export interface StageAttemptStat {
  attempts: number;
  succeeded: number;
  failed: number;
  unknown_after_interrupt: number;
  simulated: number;
}

// ------------------------------------------------- S05-04：导出与产物下载

/** 一份已登记的导出文件；`completed` 的导出必然有两份（excel + jsonl）。 */
export interface RunExportArtifact {
  artifact_id: string;
  kind: 'excel' | 'jsonl';
  download_name: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  /** 同源地址，浏览器直接下载；后端按登记行给文件，不接受前端传路径。 */
  download_url: string;
}

/**
 * 一次导出。捕获字段是**worker 认领那份导出时**的快照信息：导出还在排队或运行中
 * 时 `captured_*` 为 null（界面显示「尚未捕获快照」），不拿 `scheduled_revision`
 * 冒充已经捕获的 revision。
 */
export interface RunExport {
  export_id: string;
  job_id: string;
  run_id: string;
  source: 'automatic' | 'manual';
  job_status: JobStatus | null;
  created_at: string;
  scheduled_revision: number;
  captured_at: string | null;
  run_revision: number | null;
  run_status_at_capture: JobStatus | null;
  /** 捕获时的批次计数；`remaining` 不为 0 说明这份导出含未处理记录。 */
  counts_at_capture: RunCounts | null;
  artifacts: RunExportArtifact[];
  error: { code?: string; message?: string; error_type?: string } | null;
}

export interface ExportListPage {
  run_id: string;
  page: number;
  page_size: number;
  total: number;
  items: RunExport[];
}

/** POST /api/runs/{run_id}/exports 的返回：只表示**已入队**，文件由 worker 生成。 */
export interface ExportCreated {
  export_id: string;
  job_id: string;
  reused: boolean;
}

export interface RunDetail {
  run_id: string;
  original_filename: string;
  sheet_name: string;
  status: JobStatus;
  revision: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  counts: RunCounts;
  progress_percent: number;
  active_job: RunJobSummary | null;
  recent_jobs: RunJobSummary[];
  execution_control: RunExecutionControl;
  model_config: Record<string, unknown>;
  versions: Record<string, unknown>;
  last_error: { code?: string; message?: string; failed_count?: number } | null;
  allowed_actions: RunAllowedActions;
  failure_summary: RunFailureSummary;
  call_statistics: { stage1: StageAttemptStat; stage2: StageAttemptStat };
  /** 最近一次导出（含排队中与失败）；一次都没排过时为 null，界面显示「尚无导出」。 */
  latest_export: RunExport | null;
}

/** POST /api/runs 的返回：批次已排队，进度需轮询 GET /api/runs/{run_id}。 */
export interface RunCreated {
  run_id: string;
  job_id: string;
  reused: boolean;
}

/** POST /api/runs/{run_id}/resume 的返回：只表示**已入队**。 */
export interface ResumeResult {
  run_id: string;
  job_id: string;
  selected_records: number;
  renewed_campaigns: number;
  skipped_budget_exhausted: number;
  skipped_needs_new_batch: number;
  finalize_only: boolean;
  dispatch_paused: boolean;
  reused: boolean;
}

// S06-05 删除：`JobPayload` 是已删除的 `POST /api/judge` 的内存任务形状。任务查询
// 现在只走 `GET /api/jobs/{job_id}` 并返回库里的任务（`JobDetail`）。

/**
 * `GET /api/jobs/{job_id}`：库里的任务（[plan/08 §7]），形状与批次详情里的
 * `active_job`／`recent_jobs` 共用（`RunJobSummary` 多两个列表用不到但同一行本就
 * 取得到的字段：`last_activity_at`／`worker_id` 这里也有）。
 *
 * **`mode` 不是模拟／真实**：判别任务是 `initial`／`resume`／`retry_failed`，
 * 导出任务是 `automatic`／`manual`。判断模拟／真实要看批次详情的
 * `call_statistics.*.simulated` 与 `execution_control.last_worker.mode`。
 */
export interface JobDetail {
  job_id: string;
  run_id: string;
  kind: string;
  mode: string;
  status: JobStatus;
  current_record_key: string | null;
  current_stage: string | null;
  worker_id: string | null;
  created_at: string;
  started_at: string | null;
  last_activity_at: string | null;
  finished_at: string | null;
  error: { code: string; message: string } | null;
  result: { counts?: Record<string, number>; attempted?: number } | null;
}

// ------------------------------------- S06-04：记录列表与单条证据详情（plan/08 §6）

/** `records.status`；与建表 CHECK 同值。 */
export type RecordStatus =
  | 'pending'
  | 'input_invalid'
  | 'stage1_done'
  | 'completed'
  | 'failed';

export interface RecordFailurePayload {
  stage: string;
  code: string;
  message: string;
  attempt_count: number;
  retryable: boolean;
}

/**
 * 记录列表项。**不含 `a` 与任何 ref**：列表只用来定位与筛选，
 * 完整输入与证据正文在单条详情里。
 */
export interface RunRecordListItem {
  record_key: string;
  record_id: string | null;
  source_row: number;
  order_index: number;
  q_preview: string | null;
  status: RecordStatus;
  label: string | null;
  reason: string | null;
  review_required: boolean | null;
  failure: RecordFailurePayload | null;
}

export interface RunRecordListPage {
  items: RunRecordListItem[];
  page: number;
  page_size: number;
  total: number;
  /** 生成这一页时批次的 revision；翻页时对不上就说明批次又变了。 */
  revision: number;
}

/** 输入失败行的原因；与预检报告 `row_errors[]` 同源。 */
export interface RecordInputError {
  source_row: number;
  record_id: string | null;
  reason: string;
}

/** 单条记录的完整证据；`stage1`／`stage2` 是模型返回的原文（含更正）。 */
export interface RunRecordDetail {
  run_id: string;
  record_key: string;
  record_id: string | null;
  source_row: number;
  order_index: number;
  status: RecordStatus;
  revision: number;
  input: Record<string, string | null>;
  label: string | null;
  review_required: boolean | null;
  failure: RecordFailurePayload | null;
  input_error: RecordInputError | null;
  stage1: Stage1Analysis | null;
  stage2: Stage2Judgement | null;
  attempt_summary: AttemptSummary[];
  created_at: string;
  updated_at: string;
}
