import { useEffect, useState, type FormEvent } from 'react';
import { ApiError, api, hasAuthToken, setAuthToken, type UserResult } from './api/client';
import { describeError } from './api/errors';
import { sampleInputs, type AnalysisResult } from './api/fixtures';
import JdVersionBar, { type JobNotice, type JobVersionInfo } from './components/JdVersionBar';
import StatusBadge from './components/StatusBadge';
import AnalysisPanel from './pages/AnalysisPanel';
import DocumentManager from './pages/DocumentManager';
import DocumentUpload from './pages/DocumentUpload';
import JdImport from './pages/JdImport';
import PlanPanel from './pages/PlanPanel';

const steps = ['资料准备', '目标岗位', '匹配分析', '准备计划'];
const JOB_KEY = 'careerpilot.job';
const ANALYSIS_KEY = 'careerpilot.analysis';
type Connection = 'checking' | 'connected' | 'disconnected';

function readSelectedReadyDocuments(): string[] {
  try {
    const selected: unknown = JSON.parse(
      sessionStorage.getItem('careerpilot.selected_documents') ?? '[]',
    );
    const items: unknown = JSON.parse(
      sessionStorage.getItem('careerpilot.pending_documents') ?? '[]',
    );
    if (!Array.isArray(selected) || !Array.isArray(items)) return [];
    const ready = new Set(
      items
        .filter((item) => item && item.status === 'ready' && typeof item.documentId === 'string')
        .map((item) => item.documentId),
    );
    return [
      ...new Set(selected.filter((id): id is string => typeof id === 'string' && ready.has(id))),
    ].slice(0, 20);
  } catch {
    return [];
  }
}

function readPendingJob(): { title: string; jd: string } | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem('careerpilot.pending_analysis') ?? 'null');
    return saved?.mode === 'real' && typeof saved.title === 'string' && typeof saved.jd === 'string'
      ? { title: saved.title, jd: saved.jd }
      : null;
  } catch {
    return null;
  }
}

function readSavedJob(): JobVersionInfo | null {
  try {
    const raw = sessionStorage.getItem(JOB_KEY);
    if (!raw) return null;
    const saved = JSON.parse(raw) as Partial<JobVersionInfo> | null;
    if (
      !saved ||
      typeof saved.id !== 'string' ||
      typeof saved.version !== 'number' ||
      typeof saved.title !== 'string' ||
      typeof saved.raw_jd !== 'string'
    ) {
      return null;
    }
    return {
      id: saved.id,
      version: saved.version,
      title: saved.title,
      raw_jd: saved.raw_jd,
      sample: saved.sample === true,
    };
  } catch {
    return null;
  }
}

function readSavedAnalysis(): AnalysisResult | null {
  try {
    const raw = sessionStorage.getItem(ANALYSIS_KEY);
    if (!raw) return null;
    const saved = JSON.parse(raw) as AnalysisResult | null;
    if (!saved || typeof saved.id !== 'string' || !Array.isArray(saved.matches)) return null;
    return saved;
  } catch {
    return null;
  }
}

export default function App() {
  const [step, setStep] = useState(0);
  const [connection, setConnection] = useState<Connection>('checking');
  const [dependencies, setDependencies] = useState<Record<string, string> | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeMessage, setProbeMessage] = useState('');
  const [sampleLoaded, setSampleLoaded] = useState(false);
  const [analysisVisible, setAnalysisVisible] = useState(false);
  const [initialAnalysis] = useState(() => readSavedAnalysis());
  const [hasRealAnalysis, setHasRealAnalysis] = useState(initialAnalysis !== null);
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(initialAnalysis);
  const [analysisStale, setAnalysisStale] = useState(false);
  const [job, setJob] = useState<JobVersionInfo | null>(() => readSavedJob());
  const [jobBusy, setJobBusy] = useState(false);
  const [jobNotice, setJobNotice] = useState<JobNotice | null>(null);
  const [documentIds, setDocumentIds] = useState<string[]>([]);
  const [title, setTitle] = useState(() => readPendingJob()?.title ?? '后端开发实习生');
  const [jd, setJd] = useState(() => readPendingJob()?.jd ?? '');
  const [user, setUser] = useState<UserResult | null>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [authBusy, setAuthBusy] = useState(false);
  const [authError, setAuthError] = useState('');

  function clearSessionData() {
    sessionStorage.removeItem('careerpilot.pending_documents');
    sessionStorage.removeItem('careerpilot.selected_documents');
    sessionStorage.removeItem('careerpilot.pending_analysis');
    sessionStorage.removeItem('careerpilot.owner');
    sessionStorage.removeItem(JOB_KEY);
    sessionStorage.removeItem(ANALYSIS_KEY);
    sessionStorage.removeItem('careerpilot.plan_draft');
    sessionStorage.removeItem('careerpilot.event_cursor');
    setDocumentIds([]);
    setHasRealAnalysis(false);
    setAnalysis(null);
    setAnalysisStale(false);
    setAnalysisVisible(false);
    setJob(null);
    setJobNotice(null);
    setJobBusy(false);
    setJd('');
  }

  function acceptUser(current: UserResult) {
    if (sessionStorage.getItem('careerpilot.owner') !== current.id) {
      clearSessionData();
    } else {
      setDocumentIds(readSelectedReadyDocuments());
    }
    sessionStorage.setItem('careerpilot.owner', current.id);
    setUser(current);
  }

  useEffect(() => {
    function expired() {
      setAuthToken(null);
      setUser(null);
      clearSessionData();
      setAuthError('登录已过期，请重新登录。');
      setStep(0);
    }
    window.addEventListener('careerpilot:session-expired', expired);
    return () => window.removeEventListener('careerpilot:session-expired', expired);
  }, []);

  useEffect(() => {
    if (!hasAuthToken()) return;
    let active = true;
    void api
      .me()
      .then((response) => {
        if (active) acceptUser(response.data);
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          setAuthToken(null);
          clearSessionData();
        }
        setAuthError(describeError(error).message);
      });
    return () => {
      active = false;
    };
  }, []);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError('');
    try {
      const token = await api.login({ email, password });
      setAuthToken(token.data.access_token);
      const response = await api.me();
      acceptUser(response.data);
      setPassword('');
    } catch (error) {
      setAuthToken(null);
      setAuthError(
        error instanceof ApiError && error.status === 401
          ? '邮箱或密码错误。'
          : describeError(error).message,
      );
    } finally {
      setAuthBusy(false);
    }
  }

  function logout() {
    setAuthToken(null);
    setUser(null);
    setPassword('');
    setAuthError('');
    clearSessionData();
    setStep(0);
  }

  useEffect(() => {
    let active = true;
    void Promise.all([api.live(), api.meta()])
      .then(() => {
        if (active) setConnection('connected');
      })
      .catch(() => {
        if (active) setConnection('disconnected');
      });
    return () => {
      active = false;
    };
  }, []);

  function loadSample() {
    setSampleLoaded(true);
    setAnalysisVisible(false);
    setAnalysisStale(false);
    setJob(null);
    setJobNotice(null);
    setTitle('后端开发实习生');
    setJd(sampleInputs.jdText);
    setStep(1);
  }

  function editJd(value: string) {
    setJd(value);
    setSampleLoaded(false);
    setAnalysisVisible(false);
    setJobNotice(null);
  }

  useEffect(() => {
    if (job) {
      sessionStorage.setItem(JOB_KEY, JSON.stringify(job));
    } else {
      sessionStorage.removeItem(JOB_KEY);
    }
  }, [job]);

  function applyJob(next: JobVersionInfo) {
    setJob(next);
    if (analysis) setAnalysisStale(true);
  }

  async function saveJob() {
    if (!jd.trim()) {
      setJobNotice({ tone: 'danger', text: '请先填写岗位 JD 后再保存。' });
      return;
    }
    if (!title.trim()) {
      setJobNotice({ tone: 'danger', text: '请先填写岗位名称后再保存。' });
      return;
    }
    if (!user) {
      setJobNotice({ tone: 'danger', text: '请先登录后再保存岗位。' });
      return;
    }
    setJobBusy(true);
    setJobNotice(null);
    try {
      if (job && !job.sample && job.title === title.trim()) {
        try {
          const response = await api.patchJob(job.id, {
            raw_jd: jd,
            expected_version: job.version,
          });
          applyJob({
            id: response.data.id,
            version: response.data.version,
            title: response.data.title,
            raw_jd: response.data.raw_jd,
          });
          setJobNotice({ tone: 'success', text: `岗位已保存为 v${response.data.version}。` });
        } catch (error) {
          if (error instanceof ApiError && error.status === 409) {
            const latest = await api.getJob(job.id);
            applyJob({
              id: latest.data.id,
              version: latest.data.version,
              title: latest.data.title,
              raw_jd: latest.data.raw_jd,
            });
            setJobNotice({
              tone: 'warning',
              text: `版本冲突：已加载最新版本 v${latest.data.version}，本次修改未覆盖，请核对后再保存。`,
            });
            return;
          }
          throw error;
        }
      } else {
        const created = await api.createJob({ title: title.trim(), raw_jd: jd });
        applyJob({
          id: created.data.job_id,
          version: created.data.version,
          title: title.trim(),
          raw_jd: jd,
        });
        setJobNotice({
          tone: 'success',
          text: `岗位已创建为 v${created.data.version}。`,
        });
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 501) {
        setJobNotice({
          tone: 'warning',
          text: '后端尚未实现岗位编辑接口（PATCH /jobs 返回 501），本次修改未保存。',
        });
      } else if (error instanceof ApiError && error.status === 404) {
        setJob(null);
        setJobNotice({
          tone: 'warning',
          text: '岗位不存在或不属于当前账号（404），已清除本地岗位，请重新保存。',
        });
      } else {
        setJobNotice({ tone: 'danger', text: describeError(error).message });
      }
    } finally {
      setJobBusy(false);
    }
  }

  function saveSampleJob() {
    if (!jd.trim()) {
      setJobNotice({ tone: 'danger', text: '请先填写岗位 JD 再保存。' });
      return;
    }
    const next: JobVersionInfo = {
      id: 'job_demo_local',
      version: (job?.sample ? job.version : 0) + 1,
      title: title.trim() || '示例岗位',
      raw_jd: jd,
      sample: true,
    };
    applyJob(next);
    setJobNotice({
      tone: 'success',
      text: `已保存本地样例岗位 v${next.version}（只保存在浏览器，不写入后端）。`,
    });
  }

  const jobDirty = job ? jd !== job.raw_jd || title.trim() !== job.title : false;

  async function probe() {
    setProbing(true);
    setProbeMessage('');
    try {
      const response = await api.ready();
      setDependencies(response.data.dependencies);
      setProbeMessage(
        response.data.status === 'ready'
          ? '三个开发依赖均已就绪。'
          : '部分依赖未连接，请按启动说明检查本地服务。',
      );
    } catch {
      setDependencies(null);
      setProbeMessage('无法检查依赖。请先启动后端并确认代理配置。');
    } finally {
      setProbing(false);
    }
  }

  const connectionLabel =
    connection === 'connected'
      ? '后端骨架已连接'
      : connection === 'checking'
        ? '检查后端连接…'
        : '后端未连接';
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主内容
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            C<span>.</span>
          </span>
          <div>
            <strong>CareerPilot</strong>
            <span>求职准备工作台</span>
          </div>
        </div>
        <div className="workspace-label">
          个人工作区 <span>LOCAL</span>
        </div>
        <nav aria-label="准备步骤" className="step-nav">
          {steps.map((label, index) => (
            <button
              key={label}
              className={step === index ? 'nav-item active' : 'nav-item'}
              aria-current={step === index ? 'step' : undefined}
              onClick={() => setStep(index)}
            >
              <span className="step-number">0{index + 1}</span>
              <span>{label}</span>
              {step === index && <span className="nav-indicator" aria-hidden="true" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <p className="eyebrow">M2 / 资料与计划</p>
          <p>先整理资料，再用证据理解岗位要求。</p>
          <span>资料版本、岗位编辑与准备计划已在 M2 接入。</span>
        </div>
        <div className="connection-block">
          <StatusBadge
            label={connectionLabel}
            tone={connection === 'connected' ? 'success' : 'neutral'}
          />
          <p>连接状态仅代表基础 API 可用，业务功能仍待接入。</p>
          <a href={api.docsUrl} target="_blank" rel="noreferrer">
            打开 API 文档 <span aria-hidden="true">↗</span>
          </a>
          <button
            className="text-button probe-button"
            onClick={() => void probe()}
            disabled={probing}
          >
            {probing ? '正在检查…' : '检查开发依赖'}
          </button>
          <div aria-live="polite">
            {dependencies && (
              <ul className="dependency-list">
                {Object.entries(dependencies).map(([name, status]) => (
                  <li key={name}>
                    <span>{name}</span>
                    <span className={status === 'ok' ? 'healthy' : ''}>
                      {status === 'ok' ? '已就绪' : '未就绪'}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {probeMessage && <p>{probeMessage}</p>}
          </div>
        </div>
      </aside>

      <main id="main-content" className="main-content" tabIndex={-1}>
        <header className="topbar">
          <span>
            工作台 <span aria-hidden="true">/</span> {steps[step]}
          </span>
          <StatusBadge label={user ? `已登录 · ${user.display_name}` : '本地会话 · 未登录'} />
        </header>
        <div className="page-intro">
          <p className="eyebrow">CAREERPILOT / WORKSPACE</p>
          <h1>你的求职准备，从经历开始。</h1>
          <p>整理个人资料，选择目标岗位，核对证据，再生成准备计划。</p>
        </div>
        <div className="demo-notice">
          <span className="notice-label">开发预览</span>
          <p>
            登录后可上传资料并发起分析。真实分析需要启动项目依赖并配置模型；「样例演练」可离线预览。
          </p>
        </div>

        <section className="panel session-panel" aria-labelledby="session-title">
          <h2 id="session-title">{user ? '当前账号' : '登录开发账号'}</h2>
          {user ? (
            <div className="session-current">
              <p>
                {user.display_name} · {user.email}
              </p>
              <button type="button" className="text-button" onClick={logout}>
                退出登录
              </button>
            </div>
          ) : (
            <form className="session-form" onSubmit={(event) => void login(event)}>
              <label>
                邮箱
                <input
                  type="email"
                  required
                  autoComplete="username"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <label>
                密码
                <input
                  type="password"
                  required
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
              <button type="submit" className="primary-button" disabled={authBusy}>
                {authBusy ? '登录中…' : '登录'}
              </button>
            </form>
          )}
          {authError && (
            <p className="error-box" role="alert">
              {authError}
            </p>
          )}
        </section>

        {step === 0 && (
          <section aria-labelledby="documents-title">
            <div className="section-heading">
              <div>
                <p className="eyebrow">01 / 资料准备</p>
                <h2 id="documents-title">先让经历有据可查</h2>
              </div>
              <span className="muted small">简历 + 项目说明</span>
            </div>
            <div className="document-grid">
              <div className="panel upload-panel">
                <div className="document-symbol" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="M12 16V4m-4 4 4-4 4 4M5 15v5h14v-5" />
                  </svg>
                </div>
                <h3>添加个人资料</h3>
                <p className="muted">
                  M1 支持有文本层 PDF、Markdown、TXT；上传后等待解析与索引就绪。
                </p>
                <DocumentUpload
                  key={user?.id ?? 'anonymous'}
                  userId={user?.id}
                  onReadyChange={setDocumentIds}
                />
              </div>
              <div className="panel sample-panel">
                <span className="eyebrow">无需个人资料</span>
                <h3>用一套合成资料走一遍</h3>
                <p className="muted">
                  示例简历、校园项目和后端实习 JD 已准备好，包含三种匹配情况。
                </p>
                <ul className="file-list">
                  <li>
                    <span className="file-type">MD</span>
                    <span>
                      resume-demo.md<small>虚构学生简历</small>
                    </span>
                  </li>
                  <li>
                    <span className="file-type">MD</span>
                    <span>
                      project-demo.md<small>数据库设计与缓存经历</small>
                    </span>
                  </li>
                </ul>
                <button className="primary-button" onClick={loadSample}>
                  载入合成样例 <span aria-hidden="true">→</span>
                </button>
              </div>
            </div>
            <DocumentManager userId={user?.id} />
            <div className="footnote">
              原始资料、片段与结论分开保存。缺少证据时，系统应如实标记“资料未体现”。
            </div>
          </section>
        )}

        {step === 1 && (
          <section aria-labelledby="job-title">
            <div className="section-heading">
              <div>
                <p className="eyebrow">02 / 目标岗位</p>
                <h2 id="job-title">这次，准备哪个岗位？</h2>
              </div>
              <button className="secondary-button" onClick={loadSample}>
                载入示例 JD
              </button>
            </div>
            <div className="panel job-panel">
              <label htmlFor="position" className="field-label">
                岗位名称
              </label>
              <input
                id="position"
                value={title}
                onChange={(event) => {
                  setTitle(event.target.value);
                  setSampleLoaded(false);
                  setAnalysisVisible(false);
                  setJobNotice(null);
                }}
              />
              <label htmlFor="job-description" className="field-label">
                岗位描述（JD）
              </label>
              <textarea
                id="job-description"
                value={jd}
                onChange={(event) => editJd(event.target.value)}
                placeholder="粘贴岗位职责和技能要求…"
                rows={12}
              />
              <p id="sample-guidance" className="small muted">
                {sampleLoaded
                  ? '已载入匹配的合成资料。可以查看固定样例分析。'
                  : '准备已就绪的资料后，可对岗位内容发起真实分析。'}
              </p>

              <JdVersionBar
                job={job}
                dirty={jobDirty}
                busy={jobBusy}
                notice={jobNotice}
                stale={analysisStale}
                onSave={() => void saveJob()}
                onSampleSave={saveSampleJob}
              />

              <div className="form-actions">
                <button className="text-button" onClick={() => setStep(0)}>
                  返回资料准备
                </button>

                <button
                  className="primary-button"
                  disabled={!sampleLoaded}
                  aria-describedby="sample-guidance"
                  onClick={() => {
                    setAnalysisVisible(true);
                    setStep(2);
                  }}
                >
                  查看样例分析 <span aria-hidden="true">→</span>
                </button>
              </div>
            </div>
          </section>
        )}

        {step === 2 &&
          (analysisVisible && sampleLoaded ? (
            <AnalysisPanel />
          ) : !hasRealAnalysis ? (
            <section className="panel empty-result">
              <p className="eyebrow">03 / 匹配分析</p>
              <h2>从一份资料和一个岗位开始</h2>
              <p className="muted">先载入合成资料和示例 JD，查看带原文证据的分析页面。</p>
              <button className="primary-button" onClick={loadSample}>
                载入合成样例
              </button>
            </section>
          ) : null)}
        {step === 2 && analysis && (
          <section className="panel plan-entry">
            <div className="section-heading">
              <div>
                <p className="eyebrow">下一步</p>
                <h3>分析完成，生成准备计划</h3>
              </div>
              <button type="button" className="primary-button" onClick={() => setStep(3)}>
                生成准备计划 →
              </button>
            </div>
            <p className="small muted">
              根据分析结果、目标日期和每天可用时间生成计划草稿；确认前不会创建正式任务。
            </p>
            {analysisStale && (
              <p className="error-text small" role="status">
                岗位 JD 已更新，上次分析可能已过期；请重新分析后再生成计划。
              </p>
            )}
          </section>
        )}
        <div
          className="panel"
          hidden={step === 0 || step === 3 || (step === 2 && (!hasRealAnalysis || analysisVisible))}
        >
          {(!hasAuthToken() || user) && (
            <JdImport
              key={user?.id ?? 'anonymous'}
              jd={jd}
              title={title}
              documentIds={documentIds}
              job={job}
              jobDirty={jobDirty}
              onJobCreated={(created) => {
                setJob(created);
                setJobNotice(null);
              }}
              onAnalysis={(result) => {
                if (result) {
                  sessionStorage.setItem(ANALYSIS_KEY, JSON.stringify(result));
                } else {
                  sessionStorage.removeItem(ANALYSIS_KEY);
                }
                setAnalysis(result);
                setHasRealAnalysis(result !== null);
                setAnalysisStale(false);
                if (result) setAnalysisVisible(false);
              }}
              onGoToPlan={() => setStep(3)}
            />
          )}
        </div>
        {step === 3 && <PlanPanel analysisId={analysis?.id} analysisTitle={title} />}
        <footer className="page-footer">
          <span>CareerPilot · CS353FZ</span>
          <span>开发预览</span>
        </footer>
      </main>
    </div>
  );
}
