import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { api } from '../api/client';
import { describeError, isAbortError } from '../api/errors';
import StatusBadge from '../components/StatusBadge';

type Kind = 'resume' | 'project';

interface UploadItem {
  key: string;
  documentId: string;
  kind: Kind;
  filename: string;
  status: 'processing' | 'ready' | 'failed';
  error?: string;
  createdAt: number;
}

interface DocumentUploadProps {
  /** 就绪资料的 document_id 列表变化时回调，供父组件传给分析流程。 */
  onReadyChange?: (readyIds: string[]) => void;
}

const MAX_BYTES = 10 * 1024 * 1024; // 10 MiB
const ALLOWED = ['.pdf', '.md', '.txt'];
const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 120_000;
const STORAGE_KEY = 'careerpilot.pending_documents';

const statusLabels = {
  processing: '处理中',
  ready: '已就绪',
  failed: '失败',
} as const;

const statusTones = {
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

function mergeItems(prev: UploadItem[], updates: UploadItem[]): UploadItem[] {
  const map = new Map(updates.map((item) => [item.key, item]));
  return prev.map((item) => map.get(item.key) ?? item);
}

export default function DocumentUpload({ onReadyChange }: DocumentUploadProps) {
  const [kind, setKind] = useState<Kind>('resume');
  const [file, setFile] = useState<File | null>(null);
  const [items, setItems] = useState<UploadItem[]>(() => readSavedItems());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const timerRef = useRef<number | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const mountedRef = useRef(true);
  const itemsRef = useRef(items);
  const onReadyChangeRef = useRef(onReadyChange);

  itemsRef.current = items;

  useEffect(() => {
    onReadyChangeRef.current = onReadyChange;
  }, [onReadyChange]);

  useEffect(() => {
    mountedRef.current = true;
    startPolling();
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, []);

  useEffect(() => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    const ready = items.filter((item) => item.status === 'ready').map((item) => item.documentId);
    onReadyChangeRef.current?.(ready);
  }, [items]);

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
    const processing = itemsRef.current.filter((item) => item.status === 'processing');
    if (processing.length === 0) return;
    const now = Date.now();
    const updates: UploadItem[] = [];
    for (const item of processing) {
      if (now - item.createdAt > POLL_TIMEOUT_MS) {
        updates.push({ ...item, status: 'failed', error: '处理超时，请稍后重试。' });
        continue;
      }
      try {
        const res = await api.getDocument(item.documentId);
        const doc = res.data;
        if (doc.status === 'ready') {
          updates.push({ ...item, status: 'ready', error: undefined });
        } else if (doc.status === 'failed') {
          updates.push({ ...item, status: 'failed', error: doc.error ?? '解析或索引失败。' });
        }
      } catch (err) {
        if (isAbortError(err)) continue;
        updates.push({ ...item, status: 'failed', error: describeError(err).message });
      }
    }
    if (updates.length > 0) {
      setItems((prev) => mergeItems(prev, updates));
    }
  }

  function onSelect(event: ChangeEvent<HTMLInputElement>) {
    setFile(event.target.files?.[0] ?? null);
    setError(null);
  }

  async function upload() {
    if (!file) {
      setError('请先选择文件。');
      return;
    }
    if (file.size > MAX_BYTES) {
      setError('文件过大：单文件最大 10 MiB。');
      return;
    }
    const lower = file.name.toLowerCase();
    if (!ALLOWED.some((ext) => lower.endsWith(ext))) {
      setError('不支持的文件类型：仅支持 .pdf / .md / .txt。');
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await api.uploadDocument(file, kind);
      const item: UploadItem = {
        key: `${kind}-${res.data.document_id}`,
        documentId: res.data.document_id,
        kind,
        filename: file.name,
        status: 'processing',
        createdAt: Date.now(),
      };
      setItems((prev) => [item, ...prev]);
      if (inputRef.current) inputRef.current.value = '';
      setFile(null);
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="document-upload">
      <fieldset className="kind-picker">
        <legend className="field-label">资料类型</legend>
        <label>
          <input
            type="radio"
            name="document-kind"
            value="resume"
            checked={kind === 'resume'}
            onChange={() => setKind('resume')}
          />
          简历（resume）
        </label>
        <label>
          <input
            type="radio"
            name="document-kind"
            value="project"
            checked={kind === 'project'}
            onChange={() => setKind('project')}
          />
          项目说明（project）
        </label>
      </fieldset>

      <div className="upload-controls">
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.md,.txt"
          aria-describedby="upload-hint"
          onChange={onSelect}
        />
        <button
          type="button"
          className="primary-button"
          onClick={() => void upload()}
          disabled={busy || !file}
        >
          {busy ? '上传中…' : '上传资料'}
        </button>
      </div>
      <p id="upload-hint" className="small muted">
        支持 .pdf / .md / .txt，单文件 ≤ 10 MiB；PDF 需要有文本层。
      </p>
      {file && <p className="small">已选择 {file.name}</p>}
      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}

      {items.length > 0 && (
        <ul className="upload-list">
          {items.map((item) => (
            <li key={item.key}>
              <span className="file-type">{extOf(item.filename)}</span>
              <span className="upload-name">{item.filename}</span>
              <StatusBadge label={statusLabels[item.status]} tone={statusTones[item.status]} />
              {item.error && <span className="muted small">{item.error}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
