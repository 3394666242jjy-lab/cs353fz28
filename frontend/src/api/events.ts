import { apiBase, getAuthToken } from './client';
import type { ExecutionEvent, ExecutionEventData } from './m2-types';

export type ExecutionStreamErrorCode =
  | 'UNAUTHENTICATED'
  | 'RESOURCE_NOT_FOUND'
  | 'EVENT_HISTORY_EXPIRED'
  | 'STREAM_UNAVAILABLE'
  | 'NETWORK'
  | 'INVALID_STREAM';

export class ExecutionStreamError extends Error {
  constructor(
    message: string,
    public code: ExecutionStreamErrorCode,
  ) {
    super(message);
    this.name = 'ExecutionStreamError';
  }
}

export interface ExecutionStreamOptions {
  /** 最后一个成功应用的事件 ID；断线重连时作为 after_event_id。 */
  afterEventId?: string;
  signal?: AbortSignal;
  onEvent: (event: ExecutionEvent) => void;
  onError?: (error: ExecutionStreamError) => void;
  onEnd?: (reason: 'terminal' | 'closed') => void;
  /** 网络断开时的最大重连次数，默认 5。 */
  maxRetries?: number;
}

export interface ExecutionStreamHandle {
  close: () => void;
}

/**
 * 用带 Authorization 头的 fetch 读取执行事件流（M2 SSE）。
 * - 事件按 id 去重；
 * - completed / failed / waiting_for_input 后停止重连；
 * - 网络中断时用 after_event_id 续传，指数退避；
 * - 410 交给调用方先查 execution 快照再重连。
 */
export function subscribeExecutionEvents(
  executionId: string,
  options: ExecutionStreamOptions,
): ExecutionStreamHandle {
  const controller = new AbortController();
  const maxRetries = options.maxRetries ?? 5;
  const seen = new Set<string>();
  let lastEventId = options.afterEventId;
  let retries = 0;
  let closed = false;

  const onAbort = () => close();
  options.signal?.addEventListener('abort', onAbort, { once: true });

  function close(reason: 'terminal' | 'closed' = 'closed') {
    if (closed) return;
    closed = true;
    controller.abort();
    options.signal?.removeEventListener('abort', onAbort);
    options.onEnd?.(reason);
  }

  function fail(error: ExecutionStreamError) {
    options.onError?.(error);
    close();
  }

  function scheduleReconnect() {
    if (closed) return;
    if (retries >= maxRetries) {
      fail(new ExecutionStreamError('事件流多次重连失败，请刷新或稍后重试。', 'NETWORK'));
      return;
    }
    retries += 1;
    const delay = Math.min(1000 * 2 ** (retries - 1), 8000);
    window.setTimeout(() => {
      void connect();
    }, delay);
  }

  async function connect() {
    if (closed) return;
    const token = getAuthToken();
    const params = lastEventId ? `?after_event_id=${encodeURIComponent(lastEventId)}` : '';
    let response: Response;
    try {
      response = await fetch(
        `${apiBase}/api/v1/executions/${encodeURIComponent(executionId)}/events${params}`,
        {
          headers: {
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
            Accept: 'text/event-stream',
          },
          signal: controller.signal,
        },
      );
    } catch {
      if (closed || controller.signal.aborted) return;
      scheduleReconnect();
      return;
    }

    if (response.status === 401) {
      fail(new ExecutionStreamError('未登录或令牌失效，请重新登录。', 'UNAUTHENTICATED'));
      return;
    }
    if (response.status === 404) {
      fail(new ExecutionStreamError('执行任务不存在或已被删除。', 'RESOURCE_NOT_FOUND'));
      return;
    }
    if (response.status === 410) {
      fail(
        new ExecutionStreamError(
          '事件历史已过期，请先读取任务快照，再用 latest_event_id 重连。',
          'EVENT_HISTORY_EXPIRED',
        ),
      );
      return;
    }
    if (response.status === 405 || response.status === 501) {
      fail(
        new ExecutionStreamError(
          '服务端尚未提供执行事件流接口，请改用任务状态轮询。',
          'STREAM_UNAVAILABLE',
        ),
      );
      return;
    }
    if (!response.ok || !response.body) {
      scheduleReconnect();
      return;
    }

    retries = 0;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (!closed) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const raw = buffer.slice(0, boundary).replace(/\r/g, '');
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf('\n\n');

          const event = parseEventBlock(raw);
          if (!event) continue;
          if (event.id) {
            if (seen.has(event.id)) continue;
            seen.add(event.id);
            lastEventId = event.id;
          }
          options.onEvent(event);

          if (event.type === 'completed' || event.type === 'failed') {
            close('terminal');
            return;
          }
          if (event.type === 'waiting_for_input') {
            // 等待用户补充/确认时服务端可能关闭流；交给 UI 走 resume 流程。
            close('terminal');
            return;
          }
        }
      }
      if (!closed) scheduleReconnect();
    } catch {
      if (closed || controller.signal.aborted) return;
      scheduleReconnect();
    } finally {
      reader.releaseLock();
    }
  }

  if (options.signal?.aborted) {
    close();
  } else {
    void connect();
  }

  return { close };
}

function parseEventBlock(raw: string): ExecutionEvent | null {
  let id = '';
  let type = '';
  const dataLines: string[] = [];

  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue; // 心跳注释
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'id') id = value;
    else if (field === 'event') type = value;
    else if (field === 'data') dataLines.push(value);
  }

  if (!type || dataLines.length === 0) return null;
  try {
    const data = JSON.parse(dataLines.join('\n')) as ExecutionEventData;
    return {
      id: id || `${type}-${Date.now()}`,
      type: type as ExecutionEvent['type'],
      data,
    };
  } catch {
    return null;
  }
}
