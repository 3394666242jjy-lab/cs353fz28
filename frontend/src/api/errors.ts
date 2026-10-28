import { ApiError } from './client';

export interface UiError {
  code: string;
  message: string;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

export function describeError(err: unknown): UiError {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 400:
      case 422:
        return { code: 'INVALID_INPUT', message: err.message };
      case 401:
        return { code: 'UNAUTHENTICATED', message: '未登录或令牌失效，请先登录。' };
      case 404:
        return { code: 'RESOURCE_NOT_FOUND', message: '资源不存在（ID 错误或已被删除）。' };
      case 409:
        return { code: err.code ?? 'CONFLICT', message: `资料尚未就绪或版本冲突：${err.message}` };
      case 413:
        return { code: 'FILE_TOO_LARGE', message: '文件过大：单文件最大 10 MiB。' };
      case 415:
        return {
          code: 'UNSUPPORTED_FILE_TYPE',
          message: '不支持的文件类型：仅支持 .pdf / .md / .txt。',
        };
      case 501:
        return {
          code: 'NOT_IMPLEMENTED',
          message: '当前服务尚未实现此功能，请检查后端版本。',
        };
      default:
        return { code: err.code ?? `HTTP_${err.status}`, message: err.message };
    }
  }
  if (isAbortError(err)) {
    return { code: 'ABORTED', message: '请求已取消。' };
  }
  if (err instanceof TypeError) {
    return {
      code: 'NETWORK',
      message: '无法连接后端，请确认 API 已启动（默认 http://127.0.0.1:8000）。',
    };
  }
  return { code: 'UNKNOWN', message: err instanceof Error ? err.message : '未知错误。' };
}
