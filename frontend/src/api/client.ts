import type { components } from './schema';

type Envelope<T> = { data: T; request_id: string };
type Liveness = components['schemas']['Liveness'];
type Readiness = components['schemas']['Readiness'];
type Capabilities = components['schemas']['Capabilities'];
type ErrorEnvelope = components['schemas']['ErrorEnvelope'];
const base = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public requestId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function get<T>(path: string, allowedStatuses: number[] = []): Promise<Envelope<T>> {
  const response = await fetch(`${base}${path}`, { signal: AbortSignal.timeout(8000) });
  const payload = (await response.json()) as Envelope<T> | ErrorEnvelope;
  if (!response.ok && !allowedStatuses.includes(response.status)) {
    const error = 'error' in payload ? payload.error.message : '服务暂时不可用。';
    throw new ApiError(error, response.status, payload.request_id);
  }
  if (!('data' in payload)) throw new ApiError('服务返回了未知的数据格式。', response.status);
  return payload;
}

export const api = {
  live: () => get<Liveness>('/health/live'),
  ready: () => get<Readiness>('/health/ready', [503]),
  meta: () => get<Capabilities>('/api/v1/meta'),
  docsUrl: `${base}/docs`,
};
