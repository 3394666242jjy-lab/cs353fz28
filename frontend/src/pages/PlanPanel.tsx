import { useState } from 'react';
import { api } from '../api/client';
import { describeError } from '../api/errors';
import { sampleAnalysis } from '../api/fixtures';
import type { PlanResult, PlanTask } from '../api/m2-types';
import StatusBadge from '../components/StatusBadge';

interface PlanPanelProps {
  analysisId?: string;
  analysisTitle?: string;
  onConfirmed?: (plan: PlanResult) => void;
}

type Mode = 'real' | 'sample';

const TZ_PRESETS = ['Asia/Shanghai', 'Asia/Tokyo', 'Europe/London', 'America/New_York', 'UTC'];

function isoDate(offsetDays: number): string {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function buildSamplePlan(deadline: string, timezone: string, dailyMinutes: number): PlanResult {
  const dates = [1, 3, 5, 7].map((offset) => isoDate(offset));
  const tasks: PlanTask[] = sampleAnalysis.matches.map((match, index) => ({
    id: `task_demo_${index + 1}`,
    title:
      match.status === 'not_evidenced'
        ? `补充「${match.requirement}」相关资料`
        : `针对「${match.requirement}」巩固与练习`,
    requirement_ids: [match.requirement_id],
    scheduled_date: dates[index % dates.length],
    estimated_minutes: Math.min(90, Math.max(30, dailyMinutes)),
    source_ids: match.evidence_refs.map((ref) => ref.chunk_id),
    status: 'pending',
  }));
  return {
    id: 'plan_demo_001',
    analysis_id: 'analysis_demo_001',
    status: 'draft',
    deadline,
    timezone,
    daily_minutes: dailyMinutes,
    tasks,
    draft_tasks: tasks,
    warnings: ['样例计划：未调用后端，仅用于演示交互。'],
  };
}

export default function PlanPanel({ analysisId, analysisTitle, onConfirmed }: PlanPanelProps) {
  const [mode, setMode] = useState<Mode>(analysisId ? 'real' : 'sample');
  const [deadline, setDeadline] = useState(() => isoDate(14));
  const [timezone, setTimezone] = useState(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
  );
  const [dailyMinutes, setDailyMinutes] = useState(60);
  const [draft, setDraft] = useState<PlanResult | null>(null);
  const [confirmed, setConfirmed] = useState<PlanResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
    if (!Number.isInteger(dailyMinutes) || dailyMinutes < 15 || dailyMinutes > 1440) {
      return '每天可用时间需为 15–1440 分钟的整数。';
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
      const planId = await pollForPlan(accepted.data.execution_id);
      const plan = await api.getPlan(planId);
      setDraft(plan.data);
      setNotice('计划草稿已生成。确认前不会创建正式任务。');
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
      setDraft(response.data.plan);
      setConfirmed(response.data.plan);
      setNotice(
        `计划已确认：${response.data.tasks.length} 个正式任务已保存。重复确认会返回同一结果。`,
      );
      onConfirmed?.(response.data.plan);
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setBusy(false);
    }
  }

  const tasks = draft?.tasks ?? [];
  const isConfirmed = draft?.status === 'confirmed' || confirmed !== null;

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
          disabled={busy}
        >
          {busy ? '生成中…' : draft ? '重新生成草稿' : '生成计划草稿'}
        </button>
      </div>

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
              <li key={task.id} className="plan-task">
                <div className="plan-task-main">
                  <span className="plan-task-date">{task.scheduled_date}</span>
                  <span className="plan-task-title">{task.title}</span>
                </div>
                <span className="small muted">
                  {task.estimated_minutes} 分钟 · 关联 {task.requirement_ids.length} 项要求
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
                <span className="small muted">确认前不会创建正式任务。</span>
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
