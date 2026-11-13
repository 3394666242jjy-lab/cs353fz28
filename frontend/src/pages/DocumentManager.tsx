import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { describeError, isAbortError } from '../api/errors';
import { sampleInputs } from '../api/fixtures';
import type { DocumentExtraction } from '../api/m2-types';
import StatusBadge from '../components/StatusBadge';

type Kind = 'resume' | 'project';
type DocStatus = 'uploaded' | 'processing' | 'ready' | 'failed' | 'deleted';
type Mode = 'real' | 'sample';

interface DocItem {
  id: string;
  kind: Kind;
  filename: string;
  status: DocStatus;
  version: number;
  error?: string | null;
}

interface DocDetail extends DocItem {
  extractedText: string;
}

interface DocumentManagerProps {
  userId?: string;
  /** 删除资料后通知父组件，避免后续分析继续引用已删除资料。 */
  onDeleted?: (id: string) => void;
  onUpdated?: (id: string) => void;
}

const statusLabels: Record<DocStatus, string> = {
  uploaded: '已上传',
  processing: '处理中',
  ready: '已就绪',
  failed: '失败',
  deleted: '已删除',
};

const statusTones: Record<DocStatus, 'neutral' | 'success' | 'warning' | 'danger'> = {
  uploaded: 'neutral',
  processing: 'warning',
  ready: 'success',
  failed: 'danger',
  deleted: 'danger',
};

const SAMPLE_DOCS: DocItem[] = [
  {
    id: 'doc_demo_resume',
    kind: 'resume',
    filename: 'resume-demo.md',
    status: 'ready',
    version: 1,
  },
  {
    id: 'doc_demo_project',
    kind: 'project',
    filename: 'project-demo.md',
    status: 'ready',
    version: 1,
  },
];

const SAMPLE_TEXTS: Record<string, string> = {
  doc_demo_resume: sampleInputs.resumeText,
  doc_demo_project: sampleInputs.projectText,
};

function extOf(filename: string): string {
  const index = filename.lastIndexOf('.');
  return index >= 0 ? filename.slice(index + 1).toUpperCase() : 'FILE';
}

function extractionText(data: {
  extraction?: DocumentExtraction | null;
  extracted_text?: string | null;
}): string {
  const extraction = data.extraction;
  if (extraction?.kind === 'text') return extraction.text ?? '';
  if (extraction?.pages?.length) {
    return extraction.pages.map((page) => `[第 ${page.page} 页]\n${page.text}`).join('\n\n');
  }
  return data.extracted_text ?? '';
}

export default function DocumentManager({ userId, onDeleted, onUpdated }: DocumentManagerProps) {
  const [mode, setMode] = useState<Mode>(userId ? 'real' : 'sample');
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocDetail | null>(null);
  const [editText, setEditText] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [pendingIndex, setPendingIndex] = useState<{
    id: string;
    executionId: string;
    version: number;
  } | null>(null);

  useEffect(() => {
    if (!pendingIndex || !userId || mode !== 'real') return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      try {
        const { data: execution } = await api.getExecution(
          pendingIndex!.executionId,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (execution.status === 'failed' || execution.status === 'succeeded') {
          const { data: doc } = await api.getDocument(pendingIndex!.id, controller.signal);
          if (controller.signal.aborted) return;
          const update = { version: doc.current_version, status: doc.status, error: doc.error };
          setDocs((prev) =>
            prev.map((item) => (item.id === doc.id ? { ...item, ...update } : item)),
          );
          setDetail((prev) =>
            prev?.id === doc.id ? { ...prev, ...update, extractedText: extractionText(doc) } : prev,
          );
          if (execution.status === 'failed')
            setError(execution.error?.message ?? '新版本索引失败，已保留原版本。');
          else {
            setNotice(`新版本 v${doc.current_version} 已就绪；旧分析需要重新生成。`);
            onUpdated?.(doc.id);
          }
          setPendingIndex(null);
          return;
        }
        timer = window.setTimeout(() => void poll(), 1500);
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted) return;
        setError(`查询索引进度失败：${describeError(err).message}`);
        timer = window.setTimeout(() => void poll(), 3000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [pendingIndex, userId, mode]);

  useEffect(() => {
    if (userId) setMode('real');
  }, [userId]);

  useEffect(() => {
    setError(null);
    setNotice(null);
    setSelectedId(null);
    setDetail(null);
    setPendingDeleteId(null);
    setPendingIndex(null);

    if (mode === 'sample') {
      setDocs(SAMPLE_DOCS);
      setLoading(false);
      return;
    }
    if (!userId) {
      setDocs([]);
      setLoading(false);
      setError('请先在上方登录，再管理真实资料；也可以切换到「样例资料」预览。');
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    async function load() {
      try {
        const all: DocItem[] = [];
        let cursor: string | undefined;
        do {
          const response = await api.listDocuments(cursor, controller.signal);
          for (const doc of response.data.items) {
            all.push({
              id: doc.id,
              kind: doc.kind,
              filename: doc.filename,
              status: doc.status,
              version: doc.current_version,
              error: doc.error ?? null,
            });
          }
          cursor = response.data.next_cursor ?? undefined;
        } while (cursor);
        if (!controller.signal.aborted) setDocs(all);
      } catch (err) {
        if (isAbortError(err)) return;
        setError(`加载资料失败：${describeError(err).message}`);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [mode, userId]);

  async function openDoc(doc: DocItem) {
    setSelectedId(doc.id);
    setPendingDeleteId(null);
    setError(null);
    setNotice(null);

    if (mode === 'sample') {
      const text = SAMPLE_TEXTS[doc.id] ?? '';
      setDetail({ ...doc, extractedText: text });
      setEditText(text);
      return;
    }

    setBusy(true);
    try {
      const response = await api.getDocument(doc.id);
      const data = response.data;
      const text = extractionText(data);
      setDetail({
        id: data.id,
        kind: data.kind,
        filename: data.filename,
        status: data.status,
        version: data.current_version,
        error: data.error ?? null,
        extractedText: text,
      });
      setEditText(text);
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  async function refreshDetail() {
    if (!detail || mode === 'sample') return;
    await openDoc(detail);
  }

  async function saveText() {
    if (!detail) return;
    if (detail.status === 'deleted') {
      setError('已删除的资料不能保存新版本。');
      return;
    }

    if (mode === 'sample') {
      const nextVersion = detail.version + 1;
      setDetail({ ...detail, version: nextVersion, status: 'processing' });
      setDocs((prev) =>
        prev.map((doc) =>
          doc.id === detail.id ? { ...doc, version: nextVersion, status: 'processing' } : doc,
        ),
      );
      setNotice(
        `样例：已生成新版本 v${nextVersion}（未调用后端）。真实环境会异步重建索引，并把相关分析标记为过期。`,
      );
      window.setTimeout(() => {
        setDetail((prev) => (prev ? { ...prev, status: 'ready' } : prev));
        setDocs((prev) =>
          prev.map((doc) => (doc.id === detail.id ? { ...doc, status: 'ready' } : doc)),
        );
      }, 1200);
      return;
    }

    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await api.updateDocumentText(detail.id, {
        extracted_text: editText,
        expected_version: detail.version,
      });
      const nextVersion = response.data.version;
      setPendingIndex({
        id: detail.id,
        executionId: response.data.execution_id,
        version: nextVersion,
      });
      setDetail({ ...detail, version: nextVersion, status: 'processing', extractedText: editText });
      setDocs((prev) =>
        prev.map((doc) =>
          doc.id === detail.id ? { ...doc, version: nextVersion, status: 'processing' } : doc,
        ),
      );
      setNotice(
        `已提交新版本 v${nextVersion}（execution ${response.data.execution_id.slice(0, 8)}…）。索引完成后可用；相关分析会显示过期。`,
      );
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    if (!detail) return;

    if (mode === 'sample') {
      setDocs((prev) => prev.filter((doc) => doc.id !== detail.id));
      setDetail(null);
      setSelectedId(null);
      setPendingDeleteId(null);
      setNotice('样例：已删除（未调用后端）。真实环境会先阻断查询，再异步清理文件与索引。');
      return;
    }

    setBusy(true);
    setError(null);
    try {
      const response = await api.deleteDocument(detail.id);
      setDocs((prev) =>
        prev.map((doc) => (doc.id === detail.id ? { ...doc, status: 'deleted' } : doc)),
      );
      onDeleted?.(detail.id);
      setDetail(null);
      setSelectedId(null);
      setPendingDeleteId(null);
      setNotice(
        `已删除，清理状态：${response.data.purge_status}。查询已阻断；相关分析会标记为过期或来源不可用。`,
      );
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="document-manager" aria-labelledby="doc-manager-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">M2 / 资料管理</p>
          <h3 id="doc-manager-title">管理已上传的资料</h3>
        </div>
        <div className="mode-switch" role="group" aria-label="资料模式">
          <button
            type="button"
            className={mode === 'real' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'real'}
            onClick={() => setMode('real')}
          >
            真实资料
          </button>
          <button
            type="button"
            className={mode === 'sample' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'sample'}
            onClick={() => setMode('sample')}
          >
            样例资料
          </button>
        </div>
      </div>
      <p className="small muted">
        编辑会生成新版本；删除后查询立即被阻断，旧分析会显示过期或来源不可用。
        {mode === 'sample' ? ' 当前为样例模式，不调用后端。' : ''}
      </p>

      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="small notice-text" role="status">
          {notice}
        </p>
      )}
      {loading && <p className="small muted">正在加载资料…</p>}

      <div className="manager-layout">
        <ul className="manager-list">
          {docs.length === 0 && !loading ? (
            <li className="muted small">还没有资料。请先在上方上传。</li>
          ) : (
            docs.map((doc) => (
              <li key={doc.id} className={selectedId === doc.id ? 'active' : ''}>
                <button
                  type="button"
                  className="manager-open"
                  onClick={() => void openDoc(doc)}
                  disabled={busy}
                >
                  <span className="file-type">{extOf(doc.filename)}</span>
                  <span className="manager-name">{doc.filename}</span>
                  <StatusBadge label={statusLabels[doc.status]} tone={statusTones[doc.status]} />
                  <span className="small muted">v{doc.version}</span>
                </button>
              </li>
            ))
          )}
        </ul>

        <div className="manager-detail">
          {detail ? (
            <>
              <div className="panel-title">
                <h3>提取文本预览</h3>
                <span className="small muted">
                  v{detail.version} · {statusLabels[detail.status]}
                </span>
              </div>
              <p className="small muted">
                保存前请核对内容；保存会生成新版本，旧的索引与分析不会立即删除。
              </p>
              <textarea
                value={editText}
                onChange={(event) => setEditText(event.target.value)}
                rows={12}
                aria-label="提取文本"
                disabled={busy || detail.status === 'deleted'}
              />
              {detail.error && <p className="small error-text">{detail.error}</p>}
              <div className="manager-actions">
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void saveText()}
                  disabled={busy || detail.status === 'deleted'}
                >
                  {busy ? '提交中…' : '保存为新版本'}
                </button>
                {mode === 'real' && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => void refreshDetail()}
                    disabled={busy}
                  >
                    刷新状态
                  </button>
                )}
                {pendingDeleteId === detail.id ? (
                  <>
                    <button
                      type="button"
                      className="secondary-button danger"
                      onClick={() => void confirmDelete()}
                      disabled={busy}
                    >
                      确认删除
                    </button>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setPendingDeleteId(null)}
                    >
                      取消
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="secondary-button danger"
                    onClick={() => setPendingDeleteId(detail.id)}
                    disabled={busy || detail.status === 'deleted'}
                  >
                    删除资料
                  </button>
                )}
              </div>
              <p className="small muted">
                删除会先阻断查询，再异步清理原文件、片段与向量；失败可重试。相关分析会标记为过期。
              </p>
            </>
          ) : (
            <p className="muted small">从左侧选择一份资料，查看/编辑提取文本或删除。</p>
          )}
        </div>
      </div>
    </section>
  );
}
