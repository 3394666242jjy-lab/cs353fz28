import { useEffect, useId, useRef, useState } from 'react';
import { api } from '../api/client';
import { describeError, isAbortError } from '../api/errors';
import { sampleEvidence, type AnalysisResult, type Evidence } from '../api/fixtures';
import { LatestRequest } from '../api/latest-request';
import StatusBadge from './StatusBadge';
import Icon from './Icon';

const labels = {
  supported: { label: '有证据支持', tone: 'success' },
  partial: { label: '部分支持', tone: 'warning' },
  not_evidenced: { label: '资料未体现', tone: 'neutral' },
} as const;
type Match = AnalysisResult['matches'][number];
type Citation = Match['evidence_refs'][number];

export default function AnalysisResults({
  analysis,
  sample = false,
}: {
  analysis: AnalysisResult;
  sample?: boolean;
}) {
  const panelId = useId();
  const [selection, setSelection] = useState<{ match: Match; ref: Citation; index: number } | null>(
    null,
  );
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(new LatestRequest());
  const trigger = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLElement | null>(null);
  useEffect(() => () => request.current.cancel(), []);

  useEffect(() => {
    if (selection) {
      panel.current?.focus({ preventScroll: true });
      if (window.matchMedia('(max-width: 1150px)').matches)
        panel.current?.scrollIntoView({ block: 'start', behavior: 'instant' });
    } else if (trigger.current?.isConnected) {
      trigger.current.focus({ preventScroll: true });
      trigger.current.scrollIntoView({ block: 'nearest', behavior: 'instant' });
    }
  }, [selection, evidence, error]);

  function close() {
    request.current.cancel();
    setSelection(null);
    setEvidence(null);
    setError(null);
    setLoading(false);
  }
  async function open(match: Match, ref: Citation, index: number, button?: HTMLButtonElement) {
    if (button) trigger.current = button;
    const current = request.current.start();
    setSelection({ match, ref, index });
    setEvidence(null);
    setError(null);
    setLoading(true);

    try {
      const data = sample
        ? sampleEvidence.find(
            (item) => item.chunk_id === ref.chunk_id && item.version === ref.version,
          )
        : (await api.getEvidence(ref.chunk_id, ref.version, current.signal)).data;
      if (!data) throw new Error('没有找到这条原文，请选择其他引用。');
      if (current.isCurrent()) setEvidence(data);
    } catch (err) {
      if (current.isCurrent() && !isAbortError(err)) setError(describeError(err).message);
    } finally {
      if (current.isCurrent()) setLoading(false);
    }
  }
  return (
    <div className={`result-layout linked-results ${selection ? 'has-evidence' : ''}`}>
      <div className="match-list">
        {analysis.matches.map((match, index) => (
          <article
            className={`match-card ${selection?.match.requirement_id === match.requirement_id ? 'match-selected' : ''}`}
            key={match.requirement_id}
          >
            <div className="match-top">
              <span className="item-number">要求 {String(index + 1).padStart(2, '0')}</span>
              <StatusBadge {...labels[match.status]} />
            </div>
            <h3>{match.requirement}</h3>
            <p>{match.explanation}</p>
            {match.evidence_refs.length > 0 && (
              <div className="citation-list">
                {match.evidence_refs.map((ref, refIndex) => {
                  const active =
                    selection?.match.requirement_id === match.requirement_id &&
                    selection.ref.chunk_id === ref.chunk_id &&
                    selection.ref.version === ref.version;
                  return (
                    <button
                      key={`${ref.chunk_id}@${ref.version}`}
                      type="button"
                      className="citation"
                      aria-expanded={active}
                      aria-controls={panelId}
                      onClick={(event) =>
                        active ? close() : void open(match, ref, index, event.currentTarget)
                      }
                    >
                      <Icon name="documents" />
                      {active ? '正在核对原文' : '查看原文证据'}
                      {match.evidence_refs.length > 1 ? ` ${refIndex + 1}` : ''}
                    </button>
                  );
                })}
              </div>
            )}
            {match.missing_information.length > 0 && (
              <div className="missing-info">
                <strong>可补充的信息</strong>
                <ul>
                  {match.missing_information.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
          </article>
        ))}
      </div>
      <aside
        id={panelId}
        ref={panel}
        className="evidence-panel evidence-inspector"
        tabIndex={-1}
        aria-label="原文证据"
        onKeyDown={(event) => {
          if (event.key === 'Escape' && selection) {
            event.preventDefault();
            close();
          }
        }}
      >
        <div className="panel-title">
          <h3>
            <Icon name="shield" />
            原文证据
          </h3>
          {selection && (
            <button type="button" className="text-button" onClick={close}>
              返回对应结论
            </button>
          )}
        </div>
        {selection ? (
          <>
            <div className="evidence-context">
              <span>正在核对 · 要求 {String(selection.index + 1).padStart(2, '0')}</span>
              <p>{selection.match.requirement}</p>
            </div>
            <div aria-live="polite" aria-busy={loading}>
              {loading && (
                <p className="evidence-loading" role="status">
                  正在读取引用的原文…
                </p>
              )}
              {error && (
                <div className="error-box" role="alert">
                  <p>{error}</p>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => void open(selection.match, selection.ref, selection.index)}
                  >
                    重试加载原文
                  </button>
                </div>
              )}
              {evidence && (
                <div className="evidence-excerpt" key={`${evidence.chunk_id}@${evidence.version}`}>
                  <p className="source-name">{evidence.filename}</p>
                  <p className="small muted">
                    版本 {evidence.version} ·{' '}
                    {evidence.locator.kind === 'pdf'
                      ? `第 ${evidence.locator.page} 页`
                      : evidence.locator.line_start === evidence.locator.line_end
                        ? `第 ${evidence.locator.line_start} 行`
                        : `第 ${evidence.locator.line_start}–${evidence.locator.line_end} 行`}
                  </p>
                  <blockquote>
                    <mark>{evidence.quote}</mark>
                  </blockquote>
                  <p className="small muted">
                    高亮为该结论引用的原文片段。{sample ? '当前为合成样例。' : ''}
                  </p>
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="evidence-empty">
            <Icon name="documents" />
            <p>把结论与原文放在一起</p>
            <span>选择一条引用，核对出处与具体内容。</span>
          </div>
        )}
        <p className="evidence-boundary small muted">
          “资料未体现”表示缺少证据，不等于不具备这项能力。
        </p>
      </aside>
    </div>
  );
}
