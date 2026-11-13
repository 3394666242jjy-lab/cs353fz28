import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { describeError } from '../api/errors';
import type { components } from '../api/schema';

export default function ClarificationPanel({
  execution,
  onResumed,
}: {
  execution: components['schemas']['ExecutionResult'];
  onResumed: () => void;
}) {
  const [text, setText] = useState('');
  const [documents, setDocuments] = useState<components['schemas']['DocumentResult'][]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function refresh() {
    try {
      const r = await api.listDocuments();
      setDocuments(r.data.items.filter((d) => d.status === 'ready'));
    } catch (e) {
      setError(describeError(e).message);
    }
  }
  useEffect(() => {
    const c = new AbortController();
    api
      .listDocuments(undefined, c.signal)
      .then((r) => setDocuments(r.data.items.filter((d) => d.status === 'ready')))
      .catch((e) => {
        if (!c.signal.aborted) setError(describeError(e).message);
      });
    return () => c.abort();
  }, []);
  async function submit(keep: boolean) {
    const p = execution.pending_input;
    if (!p || busy) return;
    setBusy(true);
    setError('');
    try {
      await api.resumeExecution(execution.id, {
        interrupt_id: p.interrupt_id,
        checkpoint_id: p.checkpoint_id,
        answer: {
          text: keep ? '' : text,
          continue_without_evidence: keep,
          document_ids: keep ? [] : selected,
        },
      });
      onResumed();
    } catch (e) {
      setError(describeError(e).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="补充资料">
      <h3>还需要一些资料</h3>
      <p>{execution.pending_input?.question}</p>
      <p>
        先到“资料准备”上传文件，等处理完成后刷新列表。文字说明会记录下来，但不会直接当成经历证据。
      </p>
      <label>
        补充说明
        <textarea maxLength={8000} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <fieldset disabled={busy}>
        <legend>选择补充的已就绪资料</legend>
        {documents.map((d) => (
          <label key={d.id}>
            <input
              type="checkbox"
              checked={selected.includes(d.id)}
              onChange={(e) =>
                setSelected((old) =>
                  e.target.checked ? [...old, d.id] : old.filter((id) => id !== d.id),
                )
              }
            />
            {d.filename}
          </label>
        ))}
      </fieldset>
      <div className="form-actions">
        <button type="button" onClick={() => void refresh()} disabled={busy}>
          刷新资料
        </button>
        <button
          type="button"
          className="primary-button"
          disabled={busy || (!text.trim() && !selected.length)}
          onClick={() => void submit(false)}
        >
          提交补充
        </button>
        <button type="button" disabled={busy} onClick={() => void submit(true)}>
          按现有资料继续
        </button>
      </div>
      {error && (
        <p role="alert" className="error-box">
          {error}
        </p>
      )}
    </section>
  );
}
