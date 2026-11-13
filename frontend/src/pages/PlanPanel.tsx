import { useEffect, useState } from 'react';
import { ApiError, api } from '../api/client';
import { describeError } from '../api/errors';
import { sampleAnalysis } from '../api/fixtures';
import type { PlanDraftTask, PlanResult } from '../api/m2-types';
import StatusBadge from '../components/StatusBadge';

interface PlanPanelProps {
  analysisId?: string;
  analysisTitle?: string;
  onConfirmed?: (plan: PlanResult) => void;
}

type Mode = 'real' | 'sample';

const TZ_PRESETS = ['Asia/Shanghai', 'Asia/Tokyo', 'Europe/London', 'America/New_York', 'UTC'];
const PLAN_KEY = 'careerpilot.plan_draft';

interface SavedPlan {
  planId?: string;
  executionId?: string;
  analysisId?: string;
  deadline: string;
  timezone: string;
  dailyMinutes: number;
}

function readSavedPlan(): SavedPlan | null {
  try {
    const raw = sessionStorage.getItem(PLAN_KEY);
    if (!raw) return null;
    const saved = JSON.parse(raw) as Partial<SavedPlan> | null;
    if (
      !saved ||
      typeof saved.deadline !== 'string' ||
      typeof saved.timezone !== 'string' ||
      typeof saved.dailyMinutes !== 'number' ||
      (!saved.planId && !saved.executionId)
    ) {
      return null;
    }
    return {
      planId: typeof saved.planId === 'string' ? saved.planId : undefined,
      executionId: typeof saved.executionId === 'string' ? saved.executionId : undefined,
      analysisId: typeof saved.analysisId === 'string' ? saved.analysisId : undefined,
      deadline: saved.deadline,
      timezone: saved.timezone,
      dailyMinutes: saved.dailyMinutes,
    };
  } catch {
    return null;
  }
}

function writeSavedPlan(saved: SavedPlan | null) {
  if (saved) {
    sessionStorage.setItem(PLAN_KEY, JSON.stringify(saved));
  } else {
    sessionStorage.removeItem(PLAN_KEY);
  }
}

function isoDate(offsetDays: number): string {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function buildSamplePlan(deadline: string, timezone: string, dailyMinutes: number): PlanResult {
  const dates = [1, 3, 5, 7].map((offset) => isoDate(offset));
  const draftTasks: PlanDraftTask[] = sampleAnalysis.matches.map((match, index) => ({
    title:
      match.status === 'not_evidenced'
        ? `补充「${match.requirement}」相关资料`
        : `针对「${match.requirement}」巩固与练习`,
    requirement_ids: [match.requirement_id],
    scheduled_date: dates[index % dates.length],
    estimated_minutes: Math.min(90, Math.max(30, dailyMinutes)),
    source_refs: match.evidence_refs.map((ref) => ({
      document_id: ref.document_id,
      version: ref.version,
      chunk_id: ref.chunk_id,
    })),
  }));
  return {
    id: 'plan_demo_001',
    analysis_id: 'analysis_demo_001',
    execution_id: 'exec_demo_plan',
    status: 'draft',
    deadline,
    timezone,
    daily_minutes: dailyMinutes,
    draft_tasks: draftTasks,
    tasks: null,
    warnings: ['样例计划：未调用后端，仅用于演示交互。'],
  };
}

export default function PlanPanel({ analysisId, analysisTitle, onConfirmed }: PlanPanelProps) {
  const [initialSaved] = useState(() => readSavedPlan());
  const [mode, setMode] = useState<Mode>(analysisId || initialSaved ? 'real' : 'sample');
  const [deadline, setDeadline] = useState(() => initialSaved?.deadline ?? isoDate(14));
  const [timezone, setTimezone] = useState(
    () =>
      initialSaved?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'Asia/Shanghai',
  );
  const [dailyMinutes, setDailyMinutes] = useState(initialSaved?.dailyMinutes ?? 60);
  const [draft, setDraft] = useState<PlanResult | null>(null);
  const [confirmed, setConfirmed] = useState<PlanResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!initialSaved) return;
    if (initialSaved.analysisId && analysisId && initialSaved.analysisId !== analysisId) {
      writeSavedPlan(null);
      return;
    }
    let active = true;
    setMode('real');
    setDeadline(initialSaved.deadline);
    setTimezone(initialSaved.timezone);
    setDailyMinutes(initialSaved.dailyMinutes);
    setRestoring(true);
    void (async () => {
      try {
        let planId = initialSaved.planId;
        if (!planId && initialSaved.executionId) {
          planId = await pollForPlan(initialSaved.executionId);
        }
        if (!planId) throw new Error('本地计划记录缺少计划或执行 ID。');
        const response = await api.getPlan(planId);
        if (!active) return;
        setDraft(response.data);
        if (response.data.status === 'confirmed') {
          setConfirmed(response.data);
          setNotice('已恢复此前确认的计划；正式任务不会重复创建。');
        } else {
          setNotice('已恢复上次未确认的计划草稿。');
        }
        persistPlan(response.data);
      } catch (err) {
        if (!active) return;
        if (err instanceof ApiError && err.status === 404) {
          writeSavedPlan(null);
          setNotice('本地计划记录已失效，已清除。');
        } else {
          setError(`恢复上次计划失败：${describeError(err).message}`);
        }
      } finally {
        if (active) setRestoring(false);
      }
    })();
    return () => {
      active = false;
    };
    // 只在首次挂载恢复一次，避免重新生成草稿后被旧记录覆盖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (mode !== 'real' || !analysisId || !draft) return;
    if (draft.analysis_id !== analysisId) {
      setDraft(null);
      setConfirmed(null);
      setNotice(null);
      setError(null);
      writeSavedPlan(null);
    }
  }, [analysisId, draft, mode]);

  function persistPlan(plan: PlanResult) {
    writeSavedPlan({
      planId: plan.id,
      executionId: plan.execution_id,
      analysisId: plan.analysis_id,
      deadline: plan.deadline,
      timezone: plan.timezone,
      dailyMinutes: plan.daily_minutes,
    });
  }

  function switchMode(next: Mode) {
    setMode(next);
    setDraft(null);
    setConfirmed(null);
    setError(null);
    setNotice(null);
  }

  function validate(): string | null {
    if (!deadline) return '请选择目标日期。';
    if (deadline < isoDate(0)) return '目标日期不能早于今天。';
    if (!timezone.trim()) return '请填写时区，例如 Asia/Shanghai。';
    if (!Number.isInteger(dailyMinutes) || dailyMinutes < 1 || dailyMinutes > 1440) {
      return '每天可用时间需为 1–1440 分钟的整数。';
    }
    return null;
  }

  async function pollForPlan(executionId: string): Promise<string> {
    const started = Date.now();
    while (Date.now() - started < 120_000) {
      const response = await api.getExecution(executionId);
      const execution = response.data;
      if (execution.status === 'waiting_for_input') {
        const planId = execution.pending_input?.plan_id ?? execution.result_id;
        if (planId) return planId;
        throw new Error('计划草稿等待确认，但服务端未返回 plan_id。');
      }
      if (execution.status === 'succeeded') {
        if (execution.result_id) return execution.result_id;
        throw new Error('计划已生成，但服务端未返回 result_id。');
      }
      if (execution.status === 'failed') {
        throw new Error(execution.error?.message ?? '计划生成失败。');
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
    throw new Error('计划生成超时，请稍后重试。');
  }

  async function generatePlan() {
    const validation = validate();
    if (validation) {
      setError(validation);
      return;
    }

    setDraft(null);
    setConfirmed(null);
    setError(null);
    setNotice(null);

    if (mode === 'sample') {
      const plan = buildSamplePlan(deadline, timezone.trim(), dailyMinutes);
      setDraft(plan);
      setNotice('已生成样例计划草稿（未调用后端）。确认前不会创建正式任务。');
      return;
    }

    if (!analysisId) {
      setError('还没有可用的分析结果。请先完成一次真实分析，或切换到「样例计划」。');
      return;
    }

    setBusy(true);
    try {
      const accepted = await api.createPlan({
        analysis_id: analysisId,
        deadline,
        timezone: timezone.trim(),
        daily_minutes: dailyMinutes,
      });
      const executionId = accepted.data.execution_id;
      writeSavedPlan({
        executionId,
        analysisId,
        deadline,
        timezone: timezone.trim(),
        dailyMinutes,
      });
      const planId = await pollForPlan(executionId);
      const plan = await api.getPlan(planId);
      persistPlan(plan.data);
      setDraft(plan.data);
      setNotice('计划草稿已生成。确认前不会创建正式任务；刷新页面可恢复此草稿。');
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmPlan() {
    if (!draft || draft.status === 'confirmed') return;

    if (mode === 'sample') {
      const next: PlanResult = {
        ...draft,
        status: 'confirmed',
        confirmed_at: new Date().toISOString(),
        tasks: draft.draft_tasks.map((task, index) => ({
          id: `task_demo_${index + 1}`,
          plan_id: draft.id,
          title: task.title,
          requirement_ids: task.requirement_ids,
          scheduled_date: task.scheduled_date,
          estimated_minutes: task.estimated_minutes,
          status: 'todo',
          source_refs: task.source_refs,
        })),
      };
      setDraft(next);
      setConfirmed(next);
      setNotice('样例：计划已确认（未调用后端）。真实环境会保存正式任务并恢复 Agent。');
      onConfirmed?.(next);
      return;
    }

    setBusy(true);
    setError(null);
    try {
      const response = await api.confirmPlan(draft.id);
      const plan = response.data.plan;
      persistPlan(plan);
      setDraft(plan);
      setConfirmed(plan);
      setNotice(
        `计划已确认：${plan.tasks?.length ?? 0} 个正式任务已保存。${
          response.data.resumed ? ' Agent 已恢复执行。' : ''
        } 重复确认会返回同一结果，不会重复创建。`,
      );
      onConfirmed?.(plan);
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  const isConfirmed = draft?.status === 'confirmed' || confirmed !== null;
  const tasks = draft
    ? isConfirmed
      ? (draft.tasks ?? []).map((task) => ({
          key: task.id,
          title: task.title,
          requirement_ids: task.requirement_ids,
          scheduled_date: task.scheduled_date,
          estimated_minutes: task.estimated_minutes,
          status: task.status as string,
        }))
      : draft.draft_tasks.map((task, index) => ({
          key: `draft-${index}`,
          title: task.title,
          requirement_ids: task.requirement_ids,
          scheduled_date: task.scheduled_date,
          estimated_minutes: task.estimated_minutes,
          status: '',
        }))
    : [];

  return (
    <section className="plan-panel" aria-labelledby="plan-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">M2 / 准备计划</p>
          <h2 id="plan-title">把分析变成可执行的计划</h2>
        </div>
        <div className="mode-switch" role="group" aria-label="计划模式">
          <button
            type="button"
            className={mode === 'real' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'real'}
            onClick={() => switchMode('real')}
          >
            真实计划
          </button>
          <button
            type="button"
            className={mode === 'sample' ? 'primary-button' : 'text-button'}
            aria-pressed={mode === 'sample'}
            onClick={() => switchMode('sample')}
          >
            样例计划
          </button>
        </div>
      </div>

      <p className="small muted">
        {analysisId
          ? `基于分析 ${analysisId.slice(0, 8)}… 生成计划草稿；确认前不会创建正式任务。`
          : '还没有真实分析结果。可以先切换「样例计划」预览交互，或先完成一次分析。'}
        {analysisTitle ? ` 岗位：${analysisTitle}。` : ''}
        {mode === 'sample' ? ' 当前为样例模式，不调用后端。' : ''}
      </p>

      <div className="plan-form">
        <label className="plan-field">
          <span className="field-label">目标日期</span>
          <input
            type="date"
            value={deadline}
            min={isoDate(0)}
            onChange={(event) => setDeadline(event.target.value)}
          />
        </label>
        <label className="plan-field">
          <span className="field-label">时区</span>
          <input
            type="text"
            list="plan-timezones"
            value={timezone}
            onChange={(event) => setTimezone(event.target.value)}
            placeholder="Asia/Shanghai"
          />
          <datalist id="plan-timezones">
            {TZ_PRESETS.map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
        </label>
        <label className="plan-field">
          <span className="field-label">每天可用（分钟）</span>
          <input
            type="number"
            min={15}
            max={1440}
            step={15}
            value={dailyMinutes}
            onChange={(event) => setDailyMinutes(Number(event.target.value))}
          />
        </label>
        <button
          type="button"
          className="primary-button"
          onClick={() => void generatePlan()}
          disabled={busy || restoring || isConfirmed}
        >
          {busy ? '生成中…' : isConfirmed ? '已确认' : draft ? '重新生成草稿' : '生成计划草稿'}
        </button>
      </div>

      {restoring && <p className="small muted">正在恢复上次的计划…</p>}
      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="notice-text small" role="status">
          {notice}
        </p>
      )}

      {draft ? (
        <div className="plan-draft">
          <div className="plan-draft-head">
            <div>
              <p className="eyebrow">计划草稿</p>
              <h3>{isConfirmed ? '已确认的计划' : '草稿（尚未保存为正式任务）'}</h3>
            </div>
            <StatusBadge
              label={isConfirmed ? '已确认' : '草稿'}
              tone={isConfirmed ? 'success' : 'warning'}
            />
          </div>
          <p className="small muted">
            目标 {draft.deadline} · {draft.timezone} · 每天 {draft.daily_minutes} 分钟 · 共{' '}
            {tasks.length} 个任务
          </p>
          {draft.warnings?.length ? (
            <ul className="small muted">
              {draft.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}

          <ul className="plan-task-list">
            {tasks.map((task) => (
              <li key={task.key} className="plan-task">
                <div className="plan-task-main">
                  <span className="plan-task-date">{task.scheduled_date}</span>
                  <span className="plan-task-title">{task.title}</span>
                </div>
                <span className="small muted">
                  {task.estimated_minutes} 分钟 · 关联 {task.requirement_ids.length} 项要求
                  {task.status ? ` · ${task.status}` : ''}
                </span>
              </li>
            ))}
            {tasks.length === 0 && <li className="small muted">计划中没有任务。</li>}
          </ul>

          <div className="plan-actions">
            {isConfirmed ? (
              <span className="small muted">
                正式任务已保存；重复确认会返回同一结果，不会重复创建。
              </span>
            ) : (
              <>
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void confirmPlan()}
                  disabled={busy}
                >
                  {busy ? '确认中…' : '确认计划并创建任务'}
                </button>
                <span className="small muted">确认前不会创建正式任务；刷新页面可恢复此草稿。</span>
              </>
            )}
          </div>
        </div>
      ) : (
        <p className="small muted plan-empty">填写目标日期和每天可用时间，生成计划草稿后再确认。</p>
      )}
    </section>
  );
}
