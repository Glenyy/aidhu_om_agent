// S03-07：与后端最小接口交互。前端只展示后端返回的状态、标签与证据，
// 不根据理由自行推断业务类别。

import axios from 'axios';

import type {
  ExportCreated,
  ExportListPage,
  JobDetail,
  ResumeResult,
  RunCreated,
  RunDetail,
  RunListPage,
  RunRecordDetail,
  RunRecordListPage,
  SampleInfo,
  UploadData,
  ValidationData,
} from '../types/api';

const http = axios.create({
  baseURL: '/api',
  timeout: 120_000,
});

interface SuccessEnvelope<T> {
  data: T;
  request_id: string;
}

interface ErrorEnvelope {
  error: { code: string; message: string; details?: unknown };
  request_id: string;
}

/** 把后端的错误封套转成带 code 的 Error，便于界面分场景展示。 */
export class ApiError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
  }
}

function toApiError(error: unknown): ApiError {
  if (axios.isAxiosError(error)) {
    const payload = error.response?.data as ErrorEnvelope | undefined;
    if (payload?.error?.message) {
      return new ApiError(payload.error.code, payload.error.message);
    }
    return new ApiError('NETWORK', `请求后端失败：${error.message}`);
  }
  return new ApiError('UNKNOWN', String(error));
}

async function unwrap<T>(promise: Promise<{ data: SuccessEnvelope<T> }>): Promise<T> {
  try {
    const response = await promise;
    return response.data.data;
  } catch (error) {
    throw toApiError(error);
  }
}

export function uploadWorkbook(file: File): Promise<UploadData> {
  const form = new FormData();
  form.append('file', file);
  return unwrap<UploadData>(
    http.post<SuccessEnvelope<UploadData>>('/uploads', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  );
}

export function validateUpload(
  uploadId: string,
  sheetName: string | null,
): Promise<ValidationData> {
  return unwrap<ValidationData>(
    http.post<SuccessEnvelope<ValidationData>>(`/uploads/${uploadId}/validate`, {
      sheet_name: sheetName,
    }),
  );
}

/**
 * 按 `job_id` 查**库里的任务**（S06-02 起；plan/08 §7）。
 *
 * 本页当前没有调用点：批次页读 `GET /api/runs/{run_id}` 的 `active_job`／
 * `recent_jobs`，导出页读导出历史，都不需要单独查一条任务。这里保留类型化绑定，
 * 是因为后端接口存在且被测试覆盖（`tests/integration/test_api_s06_jobs.py`），
 * 删掉它只会让下次要用的人再写一遍封套解包。
 *
 * **`mode` 不是模拟／真实**——判别任务是 `initial`／`resume`／`retry_failed`、
 * 导出任务是 `automatic`／`manual`；模拟／真实只看批次详情的
 * `call_statistics.*.simulated` 与 `execution_control.last_worker.mode`。
 */
export function fetchJob(jobId: string): Promise<JobDetail> {
  return unwrap<JobDetail>(http.get<SuccessEnvelope<JobDetail>>(`/jobs/${jobId}`));
}

/** 合成样例清单：界面用它提供「下载示例文件」。 */
export function fetchSamples(): Promise<SampleInfo[]> {
  return unwrap<{ samples: SampleInfo[] }>(
    http.get<SuccessEnvelope<{ samples: SampleInfo[] }>>('/samples'),
  ).then((data) => data.samples);
}

/**
 * 样例下载地址。下载由浏览器直接处理（后端返回文件本体而非 JSON 封套），
 * 因此这里只给出 URL，不经过 axios。
 */
export function sampleDownloadUrl(name: string): string {
  return `/api/samples/${encodeURIComponent(name)}`;
}

// ------------------------------------------------- S04-07：批次列表、详情与恢复

/**
 * 生成一次用户动作的幂等键（plan/08 §4）。
 *
 * 后端按「方法 + 路径 + key」去重，所以创建与恢复各自保留一个键：网络结果不确定
 * 时用**同一个键**重试，避免同一批次排进两个判别任务。`crypto.randomUUID` 在
 * 本机页面（含 http://127.0.0.1）是安全上下文，可直接使用。
 */
export function newIdempotencyKey(): string {
  return crypto.randomUUID().replace(/-/g, '');
}

/** 创建批次：从一次 passed 预检开始整批判别。 */
export function createRun(validationId: string, idempotencyKey: string): Promise<RunCreated> {
  return unwrap<RunCreated>(
    http.post<SuccessEnvelope<RunCreated>>(
      '/runs',
      { validation_id: validationId },
      { headers: { 'Idempotency-Key': idempotencyKey } },
    ),
  );
}

/**
 * 批次列表；无筛选条件时默认第 1 页、每页 50 条。
 *
 * `status` 只在有筛选时发送：后端对未知状态返回 422，界面因此把 URL 里的非法值
 * 归一化掉再请求（见 `RunsView` 的 `queryStatus`）。
 */
export function fetchRuns(
  page = 1,
  pageSize = 50,
  status: string | null = null,
): Promise<RunListPage> {
  return unwrap<RunListPage>(
    http.get<SuccessEnvelope<RunListPage>>('/runs', {
      params: { page, page_size: pageSize, status: status ?? undefined },
    }),
  );
}

export function fetchRun(runId: string): Promise<RunDetail> {
  return unwrap<RunDetail>(http.get<SuccessEnvelope<RunDetail>>(`/runs/${runId}`));
}

/**
 * 恢复批次：`retryFailed=false` 只继续还有剩余预算的阶段，`true` 同时重开失败项的
 * 预算轮次。响应只表示**已入队**，实际执行由 worker 完成。
 */
export function resumeRun(
  runId: string,
  retryFailed: boolean,
  idempotencyKey: string,
): Promise<ResumeResult> {
  return unwrap<ResumeResult>(
    http.post<SuccessEnvelope<ResumeResult>>(
      `/runs/${runId}/resume`,
      { retry_failed: retryFailed },
      { headers: { 'Idempotency-Key': idempotencyKey } },
    ),
  );
}

// ------------------------------------------------- S05-04：导出与文件下载

/**
 * 排一份手动导出。请求体是空对象；响应只表示**已入队**，两份文件由 worker 生成，
 * 需要轮询导出历史（或详情里的 `latest_export`）看进度。
 */
export function createExport(runId: string, idempotencyKey: string): Promise<ExportCreated> {
  return unwrap<ExportCreated>(
    http.post<SuccessEnvelope<ExportCreated>>(
      `/runs/${runId}/exports`,
      {},
      { headers: { 'Idempotency-Key': idempotencyKey } },
    ),
  );
}

/** 导出历史，新→旧；含排队中与失败的导出。 */
export function fetchExports(runId: string, page = 1, pageSize = 50): Promise<ExportListPage> {
  return unwrap<ExportListPage>(
    http.get<SuccessEnvelope<ExportListPage>>(`/runs/${runId}/exports`, {
      params: { page, page_size: pageSize },
    }),
  );
}

/**
 * 产物下载地址。下载由浏览器直接处理（后端返回文件本体而非 JSON 封套），
 * 因此这里只给出 URL，不经过 axios。后端按登记行定位文件并给出下载名。
 */
export function artifactDownloadUrl(artifactId: string): string {
  return `/api/artifacts/${encodeURIComponent(artifactId)}/download`;
}

// ------------------------------------------- S06-04：记录列表与单条证据详情

export interface RecordListQuery {
  page?: number;
  pageSize?: number;
  label?: string | null;
  reviewRequired?: boolean | null;
  status?: string | null;
  recordId?: string | null;
}

/**
 * 记录列表：固定按 `order_index` 升序。筛选值只在给出时发送——后端对非法枚举
 * 返回 422，界面因此把 URL 里的非法值归一化掉再请求（见 `RunDetailView`）。
 */
export function fetchRecords(
  runId: string,
  query: RecordListQuery = {},
): Promise<RunRecordListPage> {
  const params: Record<string, string | number | boolean> = {
    page: query.page ?? 1,
    page_size: query.pageSize ?? 50,
  };
  if (query.label) params.label = query.label;
  if (query.status) params.status = query.status;
  if (query.recordId) params.record_id = query.recordId;
  if (query.reviewRequired !== null && query.reviewRequired !== undefined) {
    params.review_required = query.reviewRequired;
  }
  return unwrap<RunRecordListPage>(
    http.get<SuccessEnvelope<RunRecordListPage>>(`/runs/${runId}/records`, { params }),
  );
}

/**
 * 单条记录详情：完整输入、两个阶段的结果、失败与**被拒原文**。
 * `recordKey` 是内部 UUID，不是预检返回的可读键。
 */
export function fetchRecord(runId: string, recordKey: string): Promise<RunRecordDetail> {
  return unwrap<RunRecordDetail>(
    http.get<SuccessEnvelope<RunRecordDetail>>(
      `/runs/${runId}/records/${encodeURIComponent(recordKey)}`,
    ),
  );
}
