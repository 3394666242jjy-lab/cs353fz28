import type { components } from './schema';

type Envelope<T> = { data: T; request_id: string };
type ErrorEnvelope = components['schemas']['ErrorEnvelope'];
type Liveness = components['schemas']['Liveness'];
type Readiness = components['schemas']['Readiness'];
type Capabilities = components['schemas']['Capabilities'];
type JobCreate = components['schemas']['JobCreate'];
type JobCreated = components['schemas']['JobCreated'];
type AnalysisCreate = components['schemas']['AnalysisCreate'];
type ExecutionAccepted = components['schemas']['ExecutionAccepted'];
type ExecutionResult = components['schemas']['ExecutionResult'];
type AnalysisResult = components['schemas']['AnalysisResult'];
type Evidence = components['schemas']['Evidence'];
type DocumentAccepted = components['schemas']['DocumentAccepted'];
type DocumentResult = components['schemas']['DocumentResult'];

const base = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
const TOKEN_KEY = 'careerpilot.token';

/** 登录后由身份模块写入；M1 未实现登录时保持为空，请求不带 Authorization。 */
export function setAuthToken(token: string | null) {
  if (token) {
    sessionStorage.setItem(TOKEN_KEY, token);
  } else {
    sessionStorage.removeItem(TOKEN_KEY);
  }
}

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public requestId?: string,
    public code?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  method: 'GET' | 'POST',
  path: string,
  options: {
    body?: unknown;
    form?: FormData;
    signal?: AbortSignal;
    allowedStatuses?: number[];
  } = {},
): Promise<Envelope<T>> {
  const { body, form, signal, allowedStatuses = [] } = options;
  const headers: Record<string, string> = { ...authHeaders() };
  let payloadBody: BodyInit | undefined;
  if (form !== undefined) {
    payloadBody = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payloadBody = JSON.stringify(body);
  }
  const response = await fetch(`${base}${path}`, {
    method,
    headers,
    body: payloadBody,
    signal: signal ?? AbortSignal.timeout(30000),
  });
  const payload = (await response.json().catch(() => null)) as Envelope<T> | ErrorEnvelope | null;
  if (!response.ok && !allowedStatuses.includes(response.status)) {
    const error = payload && 'error' in payload ? payload.error : null;
    throw new ApiError(
      error?.message ?? '服务暂时不可用。',
      response.status,
      payload?.request_id,
      error?.code,
    );
  }
  if (!payload || !('data' in payload)) {
    throw new ApiError('服务返回了未知的数据格式。', response.status);
  }
  return payload;
}

export const api = {
  live: () => request<Liveness>('GET', '/health/live'),
  ready: () => request<Readiness>('GET', '/health/ready', { allowedStatuses: [503] }),
  meta: () => request<Capabilities>('GET', '/api/v1/meta'),
  uploadDocument: (file: File, kind: 'resume' | 'project', signal?: AbortSignal) => {
    const form = new FormData();
    form.append('file', file);
    form.append('kind', kind);
    return request<DocumentAccepted>('POST', '/api/v1/documents', { form, signal });
  },
  getDocument: (id: string, signal?: AbortSignal) =>
    request<DocumentResult>('GET', `/api/v1/documents/${encodeURIComponent(id)}`, { signal }),
  createJob: (body: JobCreate, signal?: AbortSignal) =>
    request<JobCreated>('POST', '/api/v1/jobs', { body, signal }),
  createAnalysis: (body: AnalysisCreate, signal?: AbortSignal) =>
    request<ExecutionAccepted>('POST', '/api/v1/analyses', { body, signal }),
  getExecution: (id: string, signal?: AbortSignal) =>
    request<ExecutionResult>('GET', `/api/v1/executions/${encodeURIComponent(id)}`, { signal }),
  getAnalysis: (id: string, signal?: AbortSignal) =>
    request<AnalysisResult>('GET', `/api/v1/analyses/${encodeURIComponent(id)}`, { signal }),
  getEvidence: (chunkId: string, version: number, signal?: AbortSignal) =>
    request<Evidence>('GET', `/api/v1/evidence/${encodeURIComponent(chunkId)}?version=${version}`, {
      signal,
    }),
  docsUrl: `${base}/docs`,
};
