import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { describeError } from '../api/errors';
import type { components } from '../api/schema';

export default function PreparationPlan({ analysisId }: { analysisId: string }) {
  const key = `careerpilot.plan.${analysisId}`;
  const [executionId, setExecutionId] = useState(() => sessionStorage.getItem(key) ?? '');
  const [plan, setPlan] = useState<components['schemas']['PlanResult'] | null>(null);
  const [deadline, setDeadline] = useState('');
  const [minutes, setMinutes] = useState(60);
  const [zone, setZone] = useState(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [terminal, setTerminal] = useState(false);
  useEffect(() => {
    if (!executionId || terminal) return;
    const c = new AbortController();
    let inFlight = false;
    async function tick() {
      if (inFlight) return;
      inFlight = true;
      try {
        const e = (await api.getExecution(executionId, c.signal)).data;
        if (e.status === 'failed') {
          setMessage(e.error?.message ?? '计划生成失败');
          setTerminal(true);
          return;
        }
        const id = e.pending_input?.plan_id ?? (e.result_type === 'plan' ? e.result_id : null);
        if (id) {
          const result = await api.getPlan(id, c.signal);
          if (!c.signal.aborted) {
            setPlan(result.data);
            setMessage(
              result.data.status === 'confirmed' ? '计划已确认' : '请检查草稿，再明确确认',
            );
          }
        } else setMessage('正在生成计划…');
        if (e.status === 'succeeded') setTerminal(true);
      } catch (e) {
        if (!c.signal.aborted) setMessage(describeError(e).message);
      } finally {
        inFlight = false;
      }
    }
    void tick();
    const timer = window.setInterval(() => void tick(), 1500);
    return () => {
      c.abort();
      window.clearInterval(timer);
    };
  }, [executionId, terminal]);
  async function create() {
    if (busy) return;
    setBusy(true);
    setMessage('');
    try {
      const r = await api.createPlan({
        analysis_id: analysisId,
        deadline,
        timezone: zone,
        daily_minutes: minutes,
      });
      sessionStorage.setItem(key, r.data.execution_id);
      setPlan(null);
      setTerminal(false);
      setExecutionId(r.data.execution_id);
    } catch (e) {
      setMessage(describeError(e).message);
    } finally {
      setBusy(false);
    }
  }
  async function confirm() {
    if (!plan || busy) return;
    setBusy(true);
    try {
      const r = await api.confirmPlan(plan.id);
      setPlan(r.data.plan);
      setMessage('已确认，正在保存执行结果…');
    } catch (e) {
      setMessage(describeError(e).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="准备计划">
      <h3>根据分析安排准备计划</h3>
      {!executionId && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <label>
            准备截止日期
            <input
              type="date"
              required
              value={deadline}
              onChange={(e) => setDeadline(e.target.value)}
            />
          </label>
          <label>
            每天可用分钟
            <input
              type="number"
              required
              min={1}
              max={1440}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
            />
          </label>
          <label>
            时区
            <input required value={zone} onChange={(e) => setZone(e.target.value)} />
          </label>
          <button className="primary-button" disabled={busy}>
            生成计划草稿
          </button>
        </form>
      )}
      <p aria-live="polite">{message}</p>
      {plan && (
        <>
          <p>
            截止 {plan.deadline} · 每天最多 {plan.daily_minutes} 分钟 · {plan.timezone}
          </p>
          <ol>
            {(plan.tasks ?? plan.draft_tasks ?? []).map((t, i) => (
              <li key={i}>
                {t.scheduled_date} · {t.title} · {t.estimated_minutes} 分钟
              </li>
            ))}
          </ol>
          {plan.status === 'draft' && (
            <>
              <p>目前只是草稿，确认后才保存为正式任务。</p>
              <button className="primary-button" disabled={busy} onClick={() => void confirm()}>
                确认这份计划
              </button>
            </>
          )}
        </>
      )}
      {terminal && !plan && (
        <button
          onClick={() => {
            sessionStorage.removeItem(key);
            setExecutionId('');
            setTerminal(false);
          }}
        >
          重新安排
        </button>
      )}
    </section>
  );
}
