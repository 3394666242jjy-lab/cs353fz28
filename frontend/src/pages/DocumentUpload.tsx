import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { api } from '../api/client';
import { describeError, isAbortError } from '../api/errors';
import StatusBadge from '../components/StatusBadge';
import Icon from '../components/Icon';
import { reconcileDocumentSnapshot } from '../api/upload-state';

type Kind = 'resume' | 'project';

interface UploadItem {
  key: string;
  documentId: string;
  kind: Kind;
  filename: string;
  status: 'uploaded' | 'processing' | 'ready' | 'failed';
  error?: string;
  createdAt: number;
  retryExecutionId?: string;
}

interface DocumentUploadProps {
  /** 就绪资料的 document_id 列表变化时回调，供父组件传给分析流程。 */
  onReadyChange?: (readyIds: string[]) => void;
  userId?: string;
  onLogin?: () => void;
  onLibraryChange?: () => void;
}

const MAX_DOCUMENTS = 20;
const MAX_BYTES = 10 * 1024 * 1024; // 10 MiB
const ALLOWED = ['.pdf', '.md', '.txt'];
const POLL_INTERVAL_MS = 1500;
const STORAGE_KEY = 'careerpilot.pending_documents';
const SELECTED_KEY = 'careerpilot.selected_documents';

const statusLabels = {
  uploaded: '等待解析',
  processing: '解析中',
  ready: '可使用',
  failed: '解析失败',
} as const;

const statusTones = {
  uploaded: 'neutral',
  processing: 'neutral',
  ready: 'success',
  failed: 'danger',
} as const;

function extOf(filename: string): string {
  const index = filename.lastIndexOf('.');
  return index >= 0 ? filename.slice(index + 1).toUpperCase() : 'FILE';
}

function readSavedItems(): UploadItem[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as UploadItem[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function readSelectedIds(): string[] {
  try {
    const parsed: unknown = JSON.parse(sessionStorage.getItem(SELECTED_KEY) ?? '[]');
    return Array.isArray(parsed)
      ? [...new Set(parsed.filter((id): id is string => typeof id === 'string'))].slice(
          0,
          MAX_DOCUMENTS,
        )
      : [];
  } catch {
    return [];
  }
}

function mergeItems(prev: UploadItem[], updates: UploadItem[]): UploadItem[] {
  const map = new Map(updates.map((item) => [item.key, item]));
  return prev.map((item) => map.get(item.key) ?? item);
}

export default function DocumentUpload({
  onReadyChange,
  userId,
  onLogin,
  onLibraryChange,
}: DocumentUploadProps) {
  const [kind, setKind] = useState<Kind>('resume');
  const [file, setFile] = useState<File | null>(null);
  const [items, setItems] = useState<UploadItem[]>(() => (userId ? readSavedItems() : []));
  const [selectedIds, setSelectedIds] = useState<string[]>(() => (userId ? readSelectedIds() : []));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploadFailed, setUploadFailed] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const controllerRef = useRef<AbortController | null>(null);
  const pollingRef = useRef(false);
  const retryRef = useRef(false);
  const dragDepth = useRef(0);

  const timerRef = useRef<number | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const mountedRef = useRef(true);
  const itemsRef = useRef(items);
  const onReadyChangeRef = useRef(onReadyChange);
  const libraryChangeRef = useRef(onLibraryChange);
  libraryChangeRef.current = onLibraryChange;

  itemsRef.current = items;

  useEffect(() => {
    onReadyChangeRef.current = onReadyChange;
  }, [onReadyChange]);

  useEffect(() => {
    mountedRef.current = true;
    controllerRef.current = new AbortController();
    startPolling();
    return () => {
      mountedRef.current = false;
      stopPolling();
      controllerRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!userId) return;
    const controller = new AbortController();
    async function loadDocuments() {
      try {
        const all: UploadItem[] = [];
        let cursor: string | undefined;
        do {
          const response = await api.listDocuments(cursor, controller.signal);
          for (const doc of response.data.items) {
            if (doc.status === 'deleted') continue;
            all.push({
              key: `${doc.kind}-${doc.id}`,
              documentId: doc.id,
              kind: doc.kind,
              filename: doc.filename,
              status:
                doc.status === 'ready'
                  ? 'ready'
                  : doc.status === 'failed'
                    ? 'failed'
                    : doc.status === 'uploaded'
                      ? 'uploaded'
                      : 'processing',
              error: doc.error ?? undefined,
              createdAt: Date.now(),
            });
          }
          cursor = response.data.next_cursor ?? undefined;
        } while (cursor);
        if (!controller.signal.aborted) {
          setItems((prev) => reconcileDocumentSnapshot(prev, all));
        }
      } catch (err) {
        if (!controller.signal.aborted) setError(`加载已有资料失败：${describeError(err).message}`);
      }
    }
    void loadDocuments();
    return () => controller.abort();
  }, [userId]);

  useEffect(() => {
    const failedIds = new Set(
      items.filter((item) => item.status === 'failed').map((item) => item.documentId),
    );
    if (failedIds.size) setSelectedIds((prev) => prev.filter((id) => !failedIds.has(id)));
  }, [items]);

  useEffect(() => {
    if (!userId) {
      onReadyChangeRef.current?.([]);
      return;
    }
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    sessionStorage.setItem(SELECTED_KEY, JSON.stringify(selectedIds));
    const ready = items
      .filter((item) => item.status === 'ready' && selectedIds.includes(item.documentId))
      .map((item) => item.documentId);
    onReadyChangeRef.current?.(ready);
  }, [items, selectedIds, userId]);

  function stopPolling() {
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }

  function startPolling() {
    if (timerRef.current != null) return;
    timerRef.current = window.setInterval(() => {
      void tick();
    }, POLL_INTERVAL_MS);
  }

  async function tick() {
    const signal = controllerRef.current?.signal;
    if (pollingRef.current || !userId || !signal || signal.aborted) return;
    const processing = itemsRef.current.filter(
      (item) => item.status === 'processing' || item.status === 'uploaded',
    );
    if (!processing.length) return;
    pollingRef.current = true;
    try {
      const updates: UploadItem[] = [];
      const deleted = new Set<string>();
      for (const item of processing) {
        try {
          if (item.retryExecutionId) {
            const { data: execution } = await api.getExecution(item.retryExecutionId, signal);
            if (signal.aborted) return;
            if (execution.status !== 'failed' && execution.status !== 'succeeded') continue;
            if (execution.status === 'failed') {
              updates.push({
                ...item,
                status: 'failed',
                error: execution.error?.message ?? '解析失败，请重试。',
                retryExecutionId: undefined,
              });
              continue;
            }
          }
          const { data: doc } = await api.getDocument(item.documentId, signal);
          if (signal.aborted) return;
          if (doc.status === 'deleted') deleted.add(item.documentId);
          else
            updates.push({
              ...item,
              status: doc.status,
              error: doc.error ?? undefined,
              retryExecutionId: undefined,
            });
        } catch (err) {
          if (signal.aborted || isAbortError(err)) return;
          updates.push({
            ...item,
            error: `暂时无法更新状态，将自动重试：${describeError(err).message}`,
          });
        }
      }
      if (!signal.aborted) {
        if (
          deleted.size ||
          updates.some(
            (update) =>
              update.status !== itemsRef.current.find((item) => item.key === update.key)?.status,
          )
        )
          libraryChangeRef.current?.();
        setItems((prev) =>
          mergeItems(prev, updates).filter((item) => !deleted.has(item.documentId)),
        );
        if (deleted.size) setSelectedIds((prev) => prev.filter((id) => !deleted.has(id)));
      }
    } finally {
      pollingRef.current = false;
    }
  }

  function selectFile(next: File | null) {
    if (busy) return;
    setError(null);
    setNotice('');
    setUploadFailed(false);
    setFile(null);
    if (!next) return;
    if (next.size > MAX_BYTES) setError('文件超过 10 MiB，请压缩后重新选择。');
    else if (!ALLOWED.some((ext) => next.name.toLowerCase().endsWith(ext)))
      setError('请选择 PDF、Markdown 或 TXT 文件。');
    else if (!next.size) setError('文件内容为空，请检查后重新选择。');
    else setFile(next);
    if (inputRef.current) inputRef.current.value = '';
  }
  function onSelect(event: ChangeEvent<HTMLInputElement>) {
    selectFile(event.target.files?.[0] ?? null);
  }
  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (busy) return;
    if (event.dataTransfer.files.length !== 1) {
      setError('每次添加一份资料，请拖入一个文件。');
      return;
    }
    selectFile(event.dataTransfer.files[0]);
  }
  async function upload() {
    if (busy || !file) return;
    if (!userId) {
      onLogin?.();
      return;
    }
    const signal = controllerRef.current?.signal;
    setError(null);
    setNotice('');
    setUploadFailed(false);
    setBusy(true);
    try {
      const res = await api.uploadDocument(
        file,
        kind,
        signal
          ? AbortSignal.any([signal, AbortSignal.timeout(60_000)])
          : AbortSignal.timeout(60_000),
      );
      if (!mountedRef.current || signal?.aborted) return;
      const item: UploadItem = {
        key: `${kind}-${res.data.document_id}`,
        documentId: res.data.document_id,
        kind,
        filename: file.name,
        status: 'uploaded',
        createdAt: Date.now(),
      };
      setItems((prev) => [
        item,
        ...prev.filter((existing) => existing.documentId !== item.documentId),
      ]);
      setSelectedIds((prev) =>
        prev.length < MAX_DOCUMENTS ? [...new Set([...prev, item.documentId])] : prev,
      );
      setNotice(
        `${file.name} 已上传。可在下方查看解析状态。${selectedIds.length >= MAX_DOCUMENTS ? '本次已选满 20 份，请取消其他选择后再勾选。' : ''}`,
      );
      if (inputRef.current) inputRef.current.value = '';
      setFile(null);
      libraryChangeRef.current?.();
    } catch (err) {
      if (!mountedRef.current || signal?.aborted || isAbortError(err)) return;
      setError(describeError(err).message);
      setUploadFailed(true);
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }
  async function retry(item: UploadItem) {
    if (retryRef.current || !userId) return;
    retryRef.current = true;
    setRetrying(item.documentId);
    const signal = controllerRef.current?.signal;
    try {
      const { data: doc } = await api.getDocument(item.documentId, signal);
      if (signal?.aborted) return;
      if (doc.status === 'deleted') {
        setItems((prev) => prev.filter((existing) => existing.documentId !== item.documentId));
        setSelectedIds((prev) => prev.filter((id) => id !== item.documentId));
        setNotice('该资料已删除，请重新选择文件。');
        return;
      }
      let retryExecutionId: string | undefined;
      if (doc.status === 'failed') {
        const accepted = await api.reindexDocument(
          item.documentId,
          { expected_version: doc.current_version },
          signal,
        );
        retryExecutionId = accepted.data.execution_id;
      }
      if (signal?.aborted) return;
      libraryChangeRef.current?.();
      const status: UploadItem['status'] = doc.status === 'failed' ? 'processing' : doc.status;
      setItems((prev) =>
        prev.map((existing) =>
          existing.key === item.key
            ? {
                ...existing,
                status,
                retryExecutionId,
                error: undefined,
              }
            : existing,
        ),
      );
      setSelectedIds((prev) =>
        prev.includes(item.documentId) || prev.length >= MAX_DOCUMENTS
          ? prev
          : [...prev, item.documentId],
      );
    } catch (err) {
      if (signal?.aborted || isAbortError(err)) return;
      setItems((prev) =>
        prev.map((existing) =>
          existing.key === item.key
            ? { ...existing, error: `重试未成功：${describeError(err).message}` }
            : existing,
        ),
      );
    } finally {
      retryRef.current = false;
      if (mountedRef.current) setRetrying(null);
    }
  }

  function toggleSelection(id: string) {
    setSelectedIds((prev) => {
      if (prev.includes(id)) return prev.filter((selected) => selected !== id);
      if (prev.length >= MAX_DOCUMENTS) return prev;
      return [...prev, id];
    });
  }

  return (
    <div className="document-upload">
      <fieldset className="kind-picker" disabled={busy}>
        <legend className="field-label">资料类型</legend>
        <label>
          <input
            type="radio"
            name="document-kind"
            value="resume"
            checked={kind === 'resume'}
            onChange={() => setKind('resume')}
          />
          简历
        </label>
        <label>
          <input
            type="radio"
            name="document-kind"
            value="project"
            checked={kind === 'project'}
            onChange={() => setKind('project')}
          />
          项目说明
        </label>
      </fieldset>

      <div
        className={`upload-controls upload-dropzone ${dragging ? 'is-dragging' : ''} ${busy ? 'is-uploading' : ''}`}
        onDragEnter={(event) => {
          event.preventDefault();
          if (!busy) {
            dragDepth.current += 1;
            setDragging(true);
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = busy ? 'none' : 'copy';
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          dragDepth.current = Math.max(0, dragDepth.current - 1);
          if (!dragDepth.current) setDragging(false);
        }}
        onDrop={onDrop}
        aria-busy={busy}
      >
        <label className="upload-picker">
          <input
            ref={inputRef}
            type="file"
            accept=".pdf,.md,.txt"
            disabled={busy}
            aria-label="选择资料文件"
            aria-describedby="upload-hint"
            onChange={onSelect}
          />
          <Icon name="documents" />
          <span className="upload-picker-copy">
            <strong>
              {busy
                ? '正在上传资料…'
                : dragging
                  ? '松开，添加这份资料'
                  : file
                    ? file.name
                    : '选择或拖入一份资料'}
            </strong>
            <small>
              {file
                ? `${(file.size / 1024).toFixed(1)} KB · ${kind === 'resume' ? '简历' : '项目说明'}`
                : 'PDF、Markdown 或 TXT'}
            </small>
          </span>
          <span className="upload-choose">浏览文件</span>
        </label>
        {file && (
          <button
            type="button"
            className="primary-button"
            onClick={() => void upload()}
            disabled={busy || !file}
          >
            {busy ? '上传中…' : !userId ? '登录后上传' : uploadFailed ? '重试上传' : '上传资料'}
          </button>
        )}
      </div>
      {busy && (
        <p className="upload-live small" role="status">
          <span />
          正在传输文件，请保持页面打开。
        </p>
      )}
      {notice && (
        <p className="notice-text small" role="status">
          {notice}
        </p>
      )}
      <p id="upload-hint" className="small muted">
        PDF / Markdown / TXT，最大 10 MiB。当前支持可复制文字的 PDF。
      </p>
      <details className="upload-requirements">
        <summary>PDF 文件要求</summary>
        <p className="small muted">
          含图片、头像或图形的 PDF 暂不支持，请先转换为纯文本或 Markdown。
        </p>
      </details>
      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}

      {items.length > 0 && (
        <>
          <p className="small muted" id="document-selection-hint" aria-live="polite">
            选择本次分析使用的资料：已选 {selectedIds.length} / {MAX_DOCUMENTS} 份，其中{' '}
            {
              items.filter(
                (item) => item.status === 'ready' && selectedIds.includes(item.documentId),
              ).length
            }{' '}
            份可用于分析。
          </p>
          <ul className="upload-list">
            {items.map((item) => (
              <li key={item.key} className={`upload-item upload-item-${item.status}`}>
                <label className="document-choice">
                  <input
                    type="checkbox"
                    checked={selectedIds.includes(item.documentId)}
                    disabled={
                      item.status === 'failed' ||
                      (!selectedIds.includes(item.documentId) &&
                        selectedIds.length >= MAX_DOCUMENTS)
                    }
                    onChange={() => toggleSelection(item.documentId)}
                    aria-describedby="document-selection-hint"
                  />
                  <span className="file-type">{extOf(item.filename)}</span>
                  <span className="upload-name">{item.filename}</span>
                </label>
                <StatusBadge label={statusLabels[item.status]} tone={statusTones[item.status]} />
                <ol className="upload-stages" aria-label={`${item.filename}处理状态`}>
                  <li className="stage-done">已上传</li>
                  <li
                    className={
                      item.status === 'ready'
                        ? 'stage-done'
                        : item.status === 'failed'
                          ? 'stage-failed'
                          : 'stage-current'
                    }
                  >
                    {item.status === 'failed'
                      ? '解析失败'
                      : item.status === 'ready'
                        ? '已解析'
                        : item.status === 'uploaded'
                          ? '等待解析'
                          : '正在解析'}
                  </li>
                  <li className={item.status === 'ready' ? 'stage-done' : ''}>
                    {item.status === 'ready' && (
                      <svg
                        className="completion-check"
                        viewBox="0 0 18 18"
                        fill="none"
                        stroke="currentColor"
                        aria-hidden="true"
                      >
                        <path d="m4 9 3 3 7-7" />
                      </svg>
                    )}
                    可使用
                  </li>
                </ol>
                {item.error && <span className="upload-item-error small">{item.error}</span>}
                {item.status === 'failed' && (
                  <button
                    type="button"
                    className="text-button upload-retry"
                    disabled={retrying !== null}
                    onClick={() => void retry(item)}
                  >
                    {retrying === item.documentId ? '正在重试…' : '重试解析'}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
