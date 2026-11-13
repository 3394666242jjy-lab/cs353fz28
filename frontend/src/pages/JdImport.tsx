import { useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api/client';

import { describeError, isAbortError, type UiError } from '../api/errors';
import { subscribeExecutionEvents } from '../api/events';
import { sampleAnalysis, sampleEvidence } from '../api/fixtures';
import StatusBadge from '../components/StatusBadge';
import type { JobVersionInfo } from '../components/JdVersionBar';
import type { ExecutionEvent } from '../api/m2-types';
import type { components } from '../api/schema';

type AnalysisResult = components['schemas']['AnalysisResult'];
type ExecutionResult = components['schemas']['ExecutionResult'];
type PendingInput = NonNullable<ExecutionResult['pending_input']>;
type Evidence = components['schemas']['Evidence'];
type EvidenceRef = components['schemas']['EvidenceRef'];

type Phase = 'idle' | 'creating' | 'polling' | 'waiting' | 'succeeded' | 'failed';
type Mode = 'real' | 'sample';

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 180_000;
const STORAGE_KEY = 'careerpilot.pending_analysis';
const CURSOR_KEY = 'careerpilot.event_cursor';

function readCursor(executionId: string): string | undefined {
  try {
    const saved = JSON.parse(sessionStorage.getItem(CURSOR_KEY) ?? 'null') as {
      executionId?: string;
      eventId?: string;
    } | null;
    return saved?.executionId === executionId && typeof saved.eventId === 'string'
      ? saved.eventId
      : undefined;
  } catch {
    return undefined;
  }
}

function saveCursor(executionId: string, eventId: string) {
  sessionStorage.setItem(CURSOR_KEY, JSON.stringify({ executionId, eventId }));
}

const resultLabels = {
  supported: { label: '有证据支持', tone: 'success' },
  partial: { label: '部分支持', tone: 'warning' },
  not_evidenced: { label: '资料未体现', tone: 'neutral' },
} as const;

const phaseLabels: Record<Phase, string> = {
  idle: '等待开始',
  creating: '正在创建岗位与分析任务…',
  polling: '分析执行中…',
  waiting: '等待补充信息',
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
  /** App 已保存的岗位版本；存在时复用，不再重复创建岗位。 */
  job?: JobVersionInfo | null;
  /** JD 或岗位名称有未保存修改时为 true，必须先保存再分析。 */
  jobDirty?: boolean;
  /** 分析开始时首次创建岗位，回传给 App 记录版本。 */
  onJobCreated?: (job: JobVersionInfo) => void;
  /** 分析成功后回调，便于父组件切换页面。 */
  onAnalysis?: (analysis: AnalysisResult | null) => void;
  /** 刷新后恢复已保存的分析结果。 */
  initialAnalysis?: AnalysisResult | null;
  /** 计划确认类等待输入跳转到计划页。 */
  onGoToPlan?: () => void;
}

function describeLocator(locator: Evidence['locator']): string {
  return locator.kind === 'pdf'
    ? `PDF 第 ${locator.page} 页`
    : `第 ${locator.line_start}–${locator.line_end} 行`;
}

function readPendingExecution(): {
  executionId: string;
  startedAt: number;
  documentIds: string[];
} | null {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as {
      mode?: Mode;
      executionId?: string;
      startedAt?: number;
      documentIds?: unknown;
    };
    if (parsed.mode === 'real' && parsed.executionId) {
      return {
        executionId: parsed.executionId,
        startedAt: parsed.startedAt ?? Date.now(),
        documentIds: Array.isArray(parsed.documentIds)
          ? parsed.documentIds.filter((id): id is string => typeof id === 'string')
          : [],
      };
    }
  } catch {
    // 本地状态损坏时直接丢弃，避免阻塞页面。
  }
  sessionStorage.removeItem(STORAGE_KEY);
  return null;
}

export default function JdImport({
  jd,
  title = '',
  documentIds = [],
  job = null,
  jobDirty = false,
  onJobCreated,
  onAnalysis,
  onGoToPlan,
  initialAnalysis = null,
}: JdImportProps) {
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
  const [streamNotice, setStreamNotice] = useState<string | null>(null);
  const [streamStage, setStreamStage] = useState<string | null>(null);
  const [pendingInput, setPendingInput] = useState<PendingInput | null>(null);
  const [resumeText, setResumeText] = useState('');
  const [continueWithoutEvidence, setContinueWithoutEvidence] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  const timerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const streamRef = useRef<{ close: () => void } | null>(null);
  const finishedRef = useRef(false);
  const fallingBackRef = useRef(false);
  const mountedRef = useRef(true);
  const executionIdRef = useRef<string | null>(null);
  const initialDocumentIdsRef = useRef<string[]>([]);
  const phaseRef = useRef<Phase>('idle');
  const onAnalysisRef = useRef(onAnalysis);
  const onJobCreatedRef = useRef(onJobCreated);
  const evidenceRequestRef = useRef(0);
  const inputRef = useRef({ jd, title, documentIds: JSON.stringify(documentIds) });

  const running = phase === 'creating' || phase === 'polling';
  const busy = running || phase === 'waiting';
  phaseRef.current = phase;

  useEffect(() => {
    if (initialAnalysis?.stale) {
      setAnalysis((current) =>
        current?.id === initialAnalysis.id ? { ...current, stale: true } : current,
      );
    }
  }, [initialAnalysis?.id, initialAnalysis?.stale]);

  useEffect(() => {
    onAnalysisRef.current = onAnalysis;
  }, [onAnalysis]);

  useEffect(() => {
    onJobCreatedRef.current = onJobCreated;
  }, [onJobCreated]);

  useEffect(() => {
    const ids = JSON.stringify(documentIds);
    const previous = inputRef.current;
    const textChanged = previous.jd !== jd || previous.title !== title;
    const documentsChanged = previous.documentIds !== ids;
    if (textChanged || (documentsChanged && phaseRef.current !== 'waiting')) {
      inputRef.current = { jd, title, documentIds: ids };
      reset();
    } else if (documentsChanged) {
      // 等待补充信息时允许用户去资料准备页面上传并勾选新资料，不取消当前恢复点。
      inputRef.current = { jd, title, documentIds: ids };
    }
  }, [jd, title, documentIds]);

  useEffect(() => {
    mountedRef.current = true;
    const saved = readPendingExecution();
    if (saved) {
      setMode('real');
      setPhase('polling');
      setStartedAt(saved.startedAt);
      executionIdRef.current = saved.executionId;
      initialDocumentIdsRef.current = saved.documentIds;
      const controller = new AbortController();
      abortRef.current = controller;
      follow(saved.executionId, controller);
    } else if (initialAnalysis) {
      const controller = new AbortController();
      abortRef.current = controller;
      api
        .getAnalysis(initialAnalysis.id, controller.signal)
        .then((response) => {
          if (controller.signal.aborted) return;
          setAnalysis(response.data);
          setPhase('succeeded');
          onAnalysisRef.current?.(response.data);
        })
        .catch(() => {
          if (!controller.signal.aborted) onAnalysisRef.current?.(null);
        });
    }
    return () => {
      mountedRef.current = false;
      finishedRef.current = true;
      evidenceRequestRef.current += 1;
      stopTimers();
      closeStream();
      abortRef.current?.abort();
    };
    // 仅用于刷新后恢复进度，挂载时执行一次。
  }, []);

  useEffect(() => {
    if (phase !== 'creating' && phase !== 'polling') return;
    const ticker = window.setInterval(() => {
      if (startedAt == null) return;
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      setElapsed(seconds);
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

  function closeStream() {
    streamRef.current?.close();
    streamRef.current = null;
  }

  function fail(next: UiError) {
    if (!mountedRef.current || finishedRef.current) return;
    finishedRef.current = true;
    stopTimers();
    closeStream();
    abortRef.current?.abort();
    sessionStorage.removeItem(STORAGE_KEY);
    sessionStorage.removeItem(CURSOR_KEY);
    setStreamNotice(null);
    setStreamStage(null);
    setPendingInput(null);
    setResumeError(null);
    setPhase('failed');
    setError(next);
  }

  function enterWaiting(executionId: string, pending: PendingInput) {
    if (!mountedRef.current) return;
    executionIdRef.current = executionId;
    stopTimers();
    closeStream();
    setPendingInput(pending);
    setPhase('waiting');
    setResumeError(null);
    setStreamNotice(null);
    setStreamStage(null);
  }

  function fallbackToPoll(executionId: string, controller: AbortController, reason: string) {
    if (finishedRef.current || fallingBackRef.current) return;
    fallingBackRef.current = true;
    closeStream();
    setStreamStage(null);
    setStreamNotice(`${reason}已自动切换为轮询任务状态。`);
    poll(executionId, controller);
  }

  function poll(executionId: string, controller: AbortController) {
    stopTimers();
    const tick = async () => {
      if (!mountedRef.current || finishedRef.current || controller.signal.aborted) return;
      try {
        const res = await api.getExecution(executionId, controller.signal);
        if (!mountedRef.current || finishedRef.current || controller.signal.aborted) return;
        const exec = res.data;
        setError(null);
        setExecution(exec);
        if (exec.status === 'succeeded') {
          stopTimers();
          void finish(exec.result_id ?? null, controller);
        } else if (exec.status === 'failed') {
          fail(exec.error ?? { code: 'EXECUTION_FAILED', message: '分析任务失败。' });
        } else if (exec.status === 'waiting_for_input') {
          if (exec.pending_input) {
            enterWaiting(executionId, exec.pending_input);
          } else {
            fail({
              code: 'WAITING_INPUT',
              message: '任务等待补充信息，但服务端未返回恢复点（interrupt/checkpoint）。',
            });
          }
        }
      } catch (err) {
        if (!mountedRef.current || finishedRef.current || controller.signal.aborted) return;
        if (isAbortError(err)) return;
        setError({
          code: 'POLL_RETRY',
          message: `暂时无法查询任务状态，将继续重试：${describeError(err).message}`,
        });
      }
    };
    void tick();
    timerRef.current = window.setInterval(() => {
      void tick();
    }, POLL_INTERVAL_MS);
  }

  async function finish(resultId: string | null, controller: AbortController) {
    if (finishedRef.current) return;
    if (!resultId) {
      fail({ code: 'MISSING_RESULT', message: '任务已成功但缺少结果 ID，无法读取分析结果。' });
      return;
    }
    finishedRef.current = true;
    stopTimers();
    closeStream();
    try {
      const res = await api.getAnalysis(resultId, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      sessionStorage.removeItem(STORAGE_KEY);
      sessionStorage.removeItem(CURSOR_KEY);
      setStreamNotice(null);
      setStreamStage(null);
      setPendingInput(null);
      setResumeError(null);
      setAnalysis(res.data);
      setPhase('succeeded');
      setSelectedKey(null);
      setSelectedEvidence(null);
      onAnalysisRef.current?.(res.data);
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(err)) return;
      finishedRef.current = false;
      fail(describeError(err));
    }
  }

  function follow(executionId: string, controller: AbortController) {
    closeStream();
    fallingBackRef.current = false;
    streamRef.current = subscribeExecutionEvents(executionId, {
      afterEventId: readCursor(executionId),
      signal: controller.signal,
      onEvent: (event: ExecutionEvent) => {
        if (!mountedRef.current || finishedRef.current || controller.signal.aborted) return;
        saveCursor(executionId, event.id);
        const data = event.data;
        if (data.stage) setStreamStage(data.stage);
        setExecution((previous) => ({
          id: executionId,
          type: previous?.type ?? 'analysis',
          status: data.status,
          state_version: previous?.state_version ?? 0,
          result_type: data.result_type ?? previous?.result_type ?? null,
          result_id: data.result_id ?? previous?.result_id ?? null,
          pending_input: previous?.pending_input ?? null,
          error: data.error ? { code: data.error.code, message: data.error.message } : null,
          latest_event_id: event.id,
        }));
        if (event.type === 'completed' || data.status === 'succeeded') {
          void finish(data.result_id ?? null, controller);
        } else if (event.type === 'failed' || data.status === 'failed') {
          fail(data.error ?? { code: 'EXECUTION_FAILED', message: '分析任务失败。' });
        } else if (event.type === 'waiting_for_input' || data.status === 'waiting_for_input') {
          if (data.pending_input) {
            enterWaiting(executionId, data.pending_input);
          } else {
            fail({
              code: 'WAITING_INPUT',
              message: '任务等待补充信息，但事件中缺少恢复点（interrupt/checkpoint）。',
            });
          }
        }
      },
      onError: (streamError) => {
        if (!mountedRef.current || finishedRef.current || controller.signal.aborted) return;
        if (streamError.code === 'UNAUTHENTICATED') {
          fail({ code: streamError.code, message: streamError.message });
          return;
        }
        fallbackToPoll(executionId, controller, `实时事件流不可用（${streamError.message}），`);
      },
      onEnd: (reason) => {
        if (!mountedRef.current || finishedRef.current || controller.signal.aborted) return;
        if (reason === 'terminal') return;
        fallbackToPoll(executionId, controller, '实时事件流已结束，');
      },
    });
  }

  async function submitResume() {
    const pending = pendingInput;
    const executionId = executionIdRef.current;
    if (!pending || !executionId) return;
    const added = documentIds.filter(
      (id) => id.trim().length > 0 && !initialDocumentIdsRef.current.includes(id),
    );
    const text = resumeText.trim();
    if (!text && !continueWithoutEvidence && added.length === 0) {
      setResumeError('请填写补充说明、勾选「不补充资料继续」，或先到资料准备页上传并勾选新资料。');
      return;
    }
    setResumeBusy(true);
    setResumeError(null);
    const controller = abortRef.current ?? new AbortController();
    abortRef.current = controller;
    try {
      await api.resumeExecution(
        executionId,
        {
          interrupt_id: pending.interrupt_id,
          checkpoint_id: pending.checkpoint_id,
          answer: {
            text,
            continue_without_evidence: continueWithoutEvidence,
            document_ids: added,
          },
        },
        controller.signal,
      );
      if (!mountedRef.current || controller.signal.aborted) return;
      const merged = [...new Set([...initialDocumentIdsRef.current, ...added])];
      initialDocumentIdsRef.current = merged;
      try {
        const raw = sessionStorage.getItem(STORAGE_KEY);
        if (raw) {
          const saved = JSON.parse(raw) as Record<string, unknown>;
          saved.documentIds = merged;
          sessionStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
        }
      } catch {
        // 本地状态损坏时忽略，不影响本次恢复。
      }
      setPendingInput(null);
      setResumeText('');
      setContinueWithoutEvidence(false);
      setResumeError(null);
      setPhase('polling');
      setStreamNotice('回答已提交，正在继续分析。');
      follow(executionId, controller);
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted || isAbortError(err)) return;
      if (err instanceof ApiError && err.status === 409) {
        setResumeError(
          err.code === 'INPUT_CONFLICT'
            ? '该问题已提交过不同回答，正在重新同步任务状态。'
            : '恢复点已失效（任务可能已恢复或结束），正在重新同步任务状态。',
        );
        setPendingInput(null);
        setPhase('polling');
        poll(executionId, controller);
        return;
      }
      setResumeError(describeError(err).message);
    } finally {
      if (mountedRef.current) setResumeBusy(false);
    }
  }

  function reset() {
    finishedRef.current = true;
    stopTimers();
    closeStream();
    abortRef.current?.abort();
    abortRef.current = null;
    fallingBackRef.current = false;
    executionIdRef.current = null;
    initialDocumentIdsRef.current = [];
    evidenceRequestRef.current += 1;
    sessionStorage.removeItem(STORAGE_KEY);
    sessionStorage.removeItem(CURSOR_KEY);
    setPhase('idle');
    setExecution(null);
    setAnalysis(null);
    setError(null);
    setStreamNotice(null);
    setStreamStage(null);
    setPendingInput(null);
    setResumeText('');
    setContinueWithoutEvidence(false);
    setResumeBusy(false);
    setResumeError(null);
    setElapsed(0);
    setStartedAt(null);
    setSelectedKey(null);
    setSelectedEvidence(null);
    setEvidenceLoading(null);
    finishedRef.current = false;
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
    if (jobDirty) {
      setError({
        code: 'UNSAVED_JOB',
        message: 'JD 或岗位名称有未保存修改，请先在「目标岗位」点击「保存岗位」再分析。',
      });
      return;
    }
    const ids = documentIds.filter((id) => id.trim().length > 0);
    if (ids.length === 0) {
      setError({
        code: 'NO_DOCUMENTS',
        message: '请先在「资料准备」选择已就绪的资料，或点「样例演练」查看交互。',
      });
      return;
    }
    reset();
    setMode('real');
    setPhase('creating');
    const started = Date.now();
    setStartedAt(started);
    initialDocumentIdsRef.current = ids;
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      let jobId = job && !job.sample ? job.id : null;
      if (!jobId) {
        const created = await api.createJob({ title: title.trim(), raw_jd: jd }, controller.signal);
        if (!mountedRef.current || controller.signal.aborted) return;
        jobId = created.data.job_id;
        onJobCreatedRef.current?.({
          id: jobId,
          version: created.data.version,
          title: title.trim(),
          raw_jd: jd,
        });
      }
      const accepted = await api.createAnalysis(
        { job_id: jobId, document_ids: ids },
        controller.signal,
      );
      if (!mountedRef.current || controller.signal.aborted) return;
      executionIdRef.current = accepted.data.execution_id;
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          mode: 'real',
          executionId: accepted.data.execution_id,
          startedAt: started,
          documentIds: ids,
          title,
          jd,
        }),
      );
      setPhase('polling');
      follow(accepted.data.execution_id, controller);
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

  const addedDocuments = documentIds.filter(
    (id) => id.trim().length > 0 && !initialDocumentIdsRef.current.includes(id),
  );

  return (
    <section className="jd-analysis" aria-labelledby="jd-analysis-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">真实分析 / 实时进度</p>
          <h3 id="jd-analysis-title">提交岗位，等待分析结果</h3>
        </div>
        <div className="mode-switch" role="group" aria-label="分析模式">
          <button
            type="button"
            className={mode === 'real' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'real'}
            disabled={busy}
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
            disabled={busy}
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
        真实分析使用本次选中的 {documentIds.length} 份已就绪资料（最多 20 份）；进度优先用 SSE
        实时事件推送，不可用时自动退回轮询。样例演练不访问后端。
      </p>

      <div className="form-actions">
        <button
          type="button"
          className="primary-button"
          onClick={() => void (mode === 'real' ? startReal() : startSample())}
          disabled={busy}
        >
          {busy
            ? phase === 'waiting'
              ? '等待回答…'
              : '分析中…'
            : phase === 'succeeded'
              ? '重新分析'
              : '开始分析'}
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
            tone={
              phase === 'failed'
                ? 'danger'
                : phase === 'succeeded'
                  ? 'success'
                  : phase === 'waiting'
                    ? 'warning'
                    : 'neutral'
            }
          />
          {phase === 'waiting' && (
            <span className="muted small">恢复点已保存；可刷新页面，回答后会继续执行。</span>
          )}
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
        {running && streamStage && (
          <p className="muted small" role="status">
            当前阶段：{streamStage}
          </p>
        )}
        {running && elapsed * 1000 > POLL_TIMEOUT_MS && (
          <p className="muted small">
            等待时间较长，任务可能仍在排队或执行；可以检查 Worker 和依赖服务。
          </p>
        )}
        {streamNotice && (
          <p className="muted small" role="status">
            {streamNotice}
          </p>
        )}
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

      {phase === 'waiting' && pendingInput && (
        <div className="clarification-panel panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">M2 / 恢复</p>
              <h3>分析需要补充信息</h3>
            </div>
            <StatusBadge label="等待回答" tone="warning" />
          </div>
          {pendingInput.kind === 'plan_confirmation' ? (
            <>
              <p>
                分析已完成，计划草稿等待确认。请到「04
                准备计划」查看草稿并确认；确认前不会创建正式任务。
              </p>
              <div className="form-actions">
                {onGoToPlan && (
                  <button type="button" className="primary-button" onClick={onGoToPlan}>
                    前往准备计划
                  </button>
                )}
                <button type="button" className="text-button" onClick={reset}>
                  稍后处理
                </button>
              </div>
            </>
          ) : (
            <>
              <p>{pendingInput.question ?? '请补充说明，或选择不补充资料继续。'}</p>
              <label className="field-label" htmlFor="resume-text">
                补充说明（可选）
              </label>
              <textarea
                id="resume-text"
                rows={3}
                maxLength={8000}
                value={resumeText}
                onChange={(event) => setResumeText(event.target.value)}
                placeholder="自由文本只作为待核实说明，不会自动成为经历证据。"
              />
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={continueWithoutEvidence}
                  onChange={(event) => setContinueWithoutEvidence(event.target.checked)}
                />
                不补充资料，按现有资料继续
              </label>
              {addedDocuments.length > 0 ? (
                <p className="small muted">
                  将补充 {addedDocuments.length} 份新资料：{addedDocuments.join('、')}
                  。请确认它们已索引完成。
                </p>
              ) : (
                <p className="small muted">
                  如需补充资料，请到「01
                  资料准备」上传并勾选；回到本页后，新资料会自动加入本次回答。
                </p>
              )}
              {resumeError && (
                <p className="error-text small" role="alert">
                  {resumeError}
                </p>
              )}
              <div className="form-actions">
                <button
                  type="button"
                  className="primary-button"
                  disabled={resumeBusy}
                  onClick={() => void submitResume()}
                >
                  {resumeBusy ? '提交中…' : '提交回答并继续'}
                </button>
                <button type="button" className="text-button" onClick={reset} disabled={resumeBusy}>
                  放弃本次分析
                </button>
              </div>
            </>
          )}
        </div>
      )}

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
