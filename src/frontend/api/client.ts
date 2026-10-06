// S03-07：与后端最小接口交互。前端只展示后端返回的状态、标签与证据，
// 不根据理由自行推断业务类别。

import axios from 'axios';

import type {
  JobPayload,
  JudgeCreated,
  JudgeMode,
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

/** 提交单条判别：后端返回 202 与任务号，结果通过 fetchJob 轮询。 */
export function judgeRecord(
  validationId: string,
  recordKey: string,
  mode: JudgeMode,
): Promise<JudgeCreated> {
  return unwrap<JudgeCreated>(
    http.post<SuccessEnvelope<JudgeCreated>>('/judge', {
      validation_id: validationId,
      record_key: recordKey,
      mode,
    }),
  );
}

export function fetchJob(jobId: string): Promise<JobPayload> {
  return unwrap<JobPayload>(http.get<SuccessEnvelope<JobPayload>>(`/jobs/${jobId}`));
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
