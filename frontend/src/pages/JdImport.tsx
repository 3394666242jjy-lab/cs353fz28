import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { describeError, isAbortError, type UiError } from '../api/errors';
import { sampleAnalysis, sampleEvidence } from '../api/fixtures';
import StatusBadge from '../components/StatusBadge';
import type { components } from '../api/schema';

type AnalysisResult = components['schemas']['AnalysisResult'];
type ExecutionResult = components['schemas']['ExecutionResult'];
type Evidence = components['schemas']['Evidence'];
type EvidenceRef = components['schemas']['EvidenceRef'];

type Phase = 'idle' | 'creating' | 'polling' | 'succeeded' | 'failed';
type Mode = 'real' | 'sample';

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 180_000;
const STORAGE_KEY = 'careerpilot.pending_analysis';

const resultLabels = {
  supported: { label: '有证据支持', tone: 'success' },
  partial: { label: '部分支持', tone: 'warning' },
  not_evidenced: { label: '资料未体现', tone: 'neutral' },
} as const;

const phaseLabels: Record<Phase, string> = {
  idle: '等待开始',
  creating: '正在创建岗位与分析任务…',
  polling: '分析执行中…',
  succeeded: '分析完成',
  failed: '分析失败',
};

interface JdImportProps {
  /** 父组件 App 传入的 JD 原文。 */
  jd: string;
  /** 岗位名称，对应 POST /jobs 的 title。 */
  title?: string;
  /** 已就绪的资料 ID；真实分析必需。 */
  documentIds?: string[];
  /** 分析成功后回调，便于父组件切换页面。 */
  onAnalysis?: (analysis: AnalysisResult | null) => void;
}

function describeLocator(locator: Evidence['locator']): string {
  return locator.kind === 'pdf'
    ? `PDF 第 ${locator.page} 页`
    : `第 ${locator.line_start}–${locator.line_end} 行`;
}

function readPendingExecution(): { executionId: string; startedAt: number } | null {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { mode?: Mode; executionId?: string; startedAt?: number };
    if (parsed.mode === 'real' && parsed.executionId) {
      return { executionId: parsed.executionId, startedAt: parsed.startedAt ?? Date.now() };
    }
  } catch {
    // 本地状态损坏时直接丢弃，避免阻塞页面。
  }
  sessionStorage.removeItem(STORAGE_KEY);
  return null;
}

export default function JdImport({ jd, title = '', documentIds = [], onAnalysis }: JdImportProps) {
  const [mode, setMode] = useState<Mode>('real');
  const [phase, setPhase] = useState<Phase>('idle');
  const [execution, setExecution] = useState<ExecutionResult | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<Evidence | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState<string | null>(null);

  const timerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const onAnalysisRef = useRef(onAnalysis);
  const evidenceRequestRef = useRef(0);
  const inputRef = useRef({ jd, title, documentIds: JSON.stringify(documentIds) });

  const running = phase === 'creating' || phase === 'polling';

  useEffect(() => {
    onAnalysisRef.current = onAnalysis;
  }, [onAnalysis]);

  useEffect(() => {
    const ids = JSON.stringify(documentIds);
    const previous = inputRef.current;
    if (previous.jd !== jd || previous.title !== title || previous.documentIds !== ids) {
      inputRef.current = { jd, title, documentIds: ids };
      reset();
    }
  }, [jd, title, documentIds]);

  useEffect(() => {
    mountedRef.current = true;
    const saved = readPendingExecution();
    if (saved) {
      setMode('real');
      setPhase('polling');
      setStartedAt(saved.startedAt);
      const controller = new AbortController();
      abortRef.current = controller;
      poll(saved.executionId, controller);
    }
    return () => {
      mountedRef.current = false;
      evidenceRequestRef.current += 1;
      stopTimers();
      abortRef.current?.abort();
    };
    // 仅用于刷新后恢复轮询，挂载时执行一次。
  }, []);

  useEffect(() => {
    if (phase !== 'creating' && phase !== 'polling') return;
    const ticker = window.setInterval(() => {
      if (startedAt == null) return;
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      setElapsed(seconds);
      if (phase === 'polling' && seconds * 1000 > POLL_TIMEOUT_MS) {
        fail({
          code: 'TOOL_TIMEOUT',
          message: `分析超时（超过 ${POLL_TIMEOUT_MS / 1000} 秒），可稍后重试或检查 Worker。`,
        });
      }
    }, 1000);
    return () => window.clearInterval(ticker);
  }, [phase, startedAt]);

  function stopTimers() {
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current);
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }

  function fail(next: UiError) {
    if (!mountedRef.current) return;
    stopTimers();
    abortRef.current?.abort();
    sessionStorage.removeItem(STORAGE_KEY);
    setPhase('failed');
    setError(next);
  }

  function poll(executionId: string, controller: AbortController) {
    const tick = async () => {
      if (!mountedRef.current || controller.signal.aborted) return;
      try {
        const res = await api.getExecution(executionId, controller.signal);
        if (!mountedRef.current || controller.signal.aborted) return;
        const exec = res.data;
        setExecution(exec);
        if (exec.status === 'succeeded') {
          stopTimers();
          void finish(exec, controller);
        } else if (exec.status === 'failed') {
          fail(exec.error ?? { code: 'EXECUTION_FAILED', message: '分析任务失败。' });
        } else if (exec.status === 'waiting_for_input') {
          fail({
            code: 'WAITING_INPUT',
            message: '分析请求补充信息。M1 不进入等待输入，若出现请按 M2 的恢复流程处理。',
          });
        }
      } catch (err) {
        if (!mountedRef.current || controller.signal.aborted || isAbortError(err)) return;
        fail(describeError(err));
      }
    };
    void tick();
    timerRef.current = window.setInterval(() => {
      void tick();
    }, POLL_INTERVAL_MS);
  }

  async function finish(exec: ExecutionResult, controller: AbortController) {
    if (!exec.result_id) {
      fail({ code: 'MISSING_RESULT', message: '任务已成功但缺少结果 ID，无法读取分析结果。' });
      return;
    }
    try {
      const res = await api.getAnalysis(exec.result_id, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      sessionStorage.removeItem(STORAGE_KEY);
      setAnalysis(res.data);
      setPhase('succeeded');
      setSelectedKey(null);
      setSelectedEvidence(null);
      onAnalysisRef.current?.(res.data);
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(err)) return;
      fail(describeError(err));
    }
  }

  function reset() {
    stopTimers();
    abortRef.current?.abort();
    abortRef.current = null;
    evidenceRequestRef.current += 1;
    sessionStorage.removeItem(STORAGE_KEY);
    setPhase('idle');
    setExecution(null);
    setAnalysis(null);
    setError(null);
    setElapsed(0);
    setStartedAt(null);
    setSelectedKey(null);
    setSelectedEvidence(null);
    setEvidenceLoading(null);
    onAnalysisRef.current?.(null);
  }

  async function startReal() {
    if (!jd.trim()) {
      setError({ code: 'EMPTY_JD', message: '请先填写岗位 JD。' });
      return;
    }
    if (!title.trim()) {
      setError({ code: 'EMPTY_TITLE', message: '请先填写岗位名称。' });
      return;
    }
    const ids = documentIds.filter((id) => id.trim().length > 0);
    if (ids.length === 0) {
      setError({
        code: 'NO_DOCUMENTS',
        message: '还没有已就绪的资料：请先登录并在「资料准备」上传资料，或点「样例演练」查看交互。',
      });
      return;
    }
    reset();
    setMode('real');
    setPhase('creating');
    const started = Date.now();
    setStartedAt(started);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const job = await api.createJob({ title: title.trim(), raw_jd: jd }, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      const accepted = await api.createAnalysis(
        { job_id: job.data.job_id, document_ids: ids },
        controller.signal,
      );
      if (!mountedRef.current || controller.signal.aborted) return;
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          mode: 'real',
          executionId: accepted.data.execution_id,
          startedAt: started,
          title,
          jd,
        }),
      );
      setPhase('polling');
      poll(accepted.data.execution_id, controller);
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(err)) return;
      fail(describeError(err));
    }
  }

  function startSample() {
    reset();
    setMode('sample');
    setPhase('creating');
    setStartedAt(Date.now());
    timerRef.current = window.setTimeout(() => {
      if (!mountedRef.current) return;
      const runningExec: ExecutionResult = {
        id: 'exec_demo_local',
        type: 'analysis',
        status: 'running',
        state_version: 2,
        result_type: null,
        result_id: null,
        pending_input: null,
        error: null,
        latest_event_id: null,
      };
      setExecution(runningExec);
      setPhase('polling');
      timerRef.current = window.setTimeout(() => {
        if (!mountedRef.current) return;
        const done: ExecutionResult = {
          id: 'exec_demo_local',
          type: 'analysis',
          status: 'succeeded',
          state_version: 3,
          result_type: 'analysis',
          result_id: sampleAnalysis.id,
          pending_input: null,
          error: null,
          latest_event_id: 'evt_demo_analysis_4',
        };
        setExecution(done);
        setAnalysis(sampleAnalysis);
        setPhase('succeeded');
      }, 1400);
    }, 700);
  }

  async function toggleEvidence(ref: EvidenceRef) {
    const key = `${ref.chunk_id}@${ref.version}`;
    const requestId = ++evidenceRequestRef.current;
    if (selectedKey === key) {
      setSelectedKey(null);
      setSelectedEvidence(null);
      setEvidenceLoading(null);
      return;
    }
    setSelectedKey(key);
    setSelectedEvidence(null);
    setEvidenceLoading(key);
    try {
      let evidence: Evidence | undefined;
      if (mode === 'sample') {
        evidence = sampleEvidence.find(
          (item) => item.chunk_id === ref.chunk_id && item.version === ref.version,
        );
        if (!evidence) throw new Error('样例数据中没有这条证据。');
      } else {
        const res = await api.getEvidence(ref.chunk_id, ref.version);
        evidence = res.data;
      }
      if (!evidence) throw new Error('没有找到对应证据。');
      if (!mountedRef.current || requestId !== evidenceRequestRef.current) return;
      setSelectedEvidence(evidence);
    } catch (err) {
      if (!mountedRef.current || requestId !== evidenceRequestRef.current) return;
      setError(describeError(err));
    } finally {
      if (mountedRef.current && requestId === evidenceRequestRef.current) setEvidenceLoading(null);
    }
  }

  return (
    <section className="jd-analysis" aria-labelledby="jd-analysis-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">真实分析 / 轮询</p>
          <h3 id="jd-analysis-title">提交岗位，等待分析结果</h3>
        </div>
        <div className="mode-switch" role="group" aria-label="分析模式">
          <button
            type="button"
            className={mode === 'real' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'real'}
            disabled={running}
            onClick={() => {
              reset();
              setMode('real');
            }}
          >
            真实分析
          </button>
          <button
            type="button"
            className={mode === 'sample' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'sample'}
            disabled={running}
            onClick={() => {
              reset();
              setMode('sample');
            }}
          >
            样例演练
          </button>
        </div>
      </div>

      <p className="small muted">
        真实分析根据已上传的资料查找证据；样例演练使用合成数据，不访问后端。
      </p>

      <div className="form-actions">
        <button
          type="button"
          className="primary-button"
          onClick={() => void (mode === 'real' ? startReal() : startSample())}
          disabled={running}
        >
          {running ? '分析中…' : phase === 'succeeded' ? '重新分析' : '开始分析'}
        </button>
        {(phase === 'succeeded' || phase === 'failed') && (
          <button type="button" className="text-button" onClick={reset}>
            重置
          </button>
        )}
      </div>

      <div className="analysis-status" aria-live="polite">
        <div className="status-line">
          <StatusBadge
            label={phaseLabels[phase]}
            tone={phase === 'failed' ? 'danger' : phase === 'succeeded' ? 'success' : 'neutral'}
          />
          {running && (
            <span className="muted small">
              已运行 {elapsed}s
              {mode === 'sample'
                ? ' · 样例模式'
                : execution
                  ? ` · 任务状态 ${execution.status}`
                  : ''}
            </span>
          )}
        </div>
        {running && (
          <progress
            max={POLL_TIMEOUT_MS / 1000}
            value={Math.min(elapsed, POLL_TIMEOUT_MS / 1000)}
            aria-label="分析进度（按已运行时间估算）"
          />
        )}
        {error && (
          <div className="error-box" role="alert">
            <strong>{error.code}</strong>
            <p>{error.message}</p>
          </div>
        )}
      </div>

      {analysis && (
        <div className="analysis-result">
          <div className="section-heading">
            <div>
              <p className="eyebrow">分析结果</p>
              <h3>共 {analysis.matches.length} 项要求</h3>
            </div>
            <span className="muted small">
              {analysis.matches.filter((item) => item.status === 'supported').length} 项有证据 ·
              {analysis.matches.filter((item) => item.status === 'partial').length} 项部分支持 ·
              {analysis.matches.filter((item) => item.status === 'not_evidenced').length} 项未体现
            </span>
          </div>
          {analysis.stale && (
            <p className="small">资料已更新，本分析可能过期（stale），请重新分析。</p>
          )}
          {analysis.warnings.length > 0 && (
            <ul className="muted small">
              {analysis.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
          <div className="result-layout">
            <div className="match-list">
              {analysis.matches.map((match, index) => (
                <article className="match-card" key={match.requirement_id}>
                  <div className="match-top">
                    <span className="item-number">要求 {String(index + 1).padStart(2, '0')}</span>
                    <StatusBadge {...resultLabels[match.status]} />
                  </div>
                  <h3>{match.requirement}</h3>
                  <p>{match.explanation}</p>
                  {match.evidence_refs.length > 0 && (
                    <div className="citation-list">
                      {match.evidence_refs.map((ref) => {
                        const key = `${ref.chunk_id}@${ref.version}`;
                        return (
                          <button
                            key={key}
                            type="button"
                            className="citation"
                            aria-expanded={selectedKey === key}
                            disabled={evidenceLoading === key}
                            onClick={() => void toggleEvidence(ref)}
                          >
                            <span aria-hidden="true">↗</span>{' '}
                            {evidenceLoading === key ? '加载证据…' : '查看原文证据'}
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
            <aside className="evidence-panel" aria-live="polite">
              {selectedEvidence ? (
                <>
                  <div className="panel-title">
                    <h3>原文证据</h3>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        evidenceRequestRef.current += 1;
                        setSelectedKey(null);
                        setSelectedEvidence(null);
                        setEvidenceLoading(null);
                      }}
                    >
                      关闭
                    </button>
                  </div>
                  <p className="source-name">{selectedEvidence.filename}</p>
                  <p className="muted small">
                    版本 {selectedEvidence.version} · {describeLocator(selectedEvidence.locator)}
                  </p>
                  <blockquote>{selectedEvidence.quote}</blockquote>
                </>
              ) : (
                <p className="muted small">点击「查看原文证据」，核对结论对应的原文位置。</p>
              )}
            </aside>
          </div>
        </div>
      )}
    </section>
  );
}
