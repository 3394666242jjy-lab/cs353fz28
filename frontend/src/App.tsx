import { useEffect, useState, type FormEvent } from 'react';
import { ApiError, api, hasAuthToken, setAuthToken, type UserResult } from './api/client';
import { describeError } from './api/errors';
import { sampleInputs, type AnalysisResult } from './api/fixtures';
import JdVersionBar, { type JobNotice, type JobVersionInfo } from './components/JdVersionBar';
import StatusBadge from './components/StatusBadge';
import Icon from './components/Icon';
import AnalysisPanel from './pages/AnalysisPanel';
import DocumentManager from './pages/DocumentManager';
import DocumentUpload from './pages/DocumentUpload';
import JdImport from './pages/JdImport';
import PlanPanel from './pages/PlanPanel';
import KnowledgeLibrary from './pages/KnowledgeLibrary';

const steps = ['资料准备', '目标岗位', '匹配分析', '准备计划', '知识素材'];
const stepIcons = ['documents', 'job', 'analysis', 'plan', 'knowledge'] as const;
const pageCopy = [
  ['让你的经历，成为准备的起点。', '整理简历与项目资料，让每一项岗位匹配都有据可查。'],
  ['找准方向，再开始准备。', '保存目标岗位的要求，用已有经历找到下一步。'],
  ['理解匹配，也看见差距。', '逐项核对岗位要求与原文证据，找到值得补充的信息。'],
  ['把准备，安排进每一天。', '设定目标日期与可用时间，预览后再确认你的计划。'],
  ['向你想去的方向，多走一步。', '从可靠来源学习，把知识转化成下一次实践的起点。'],
];
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
  const [accountOpen, setAccountOpen] = useState(false);

  useEffect(() => {
    if (accountOpen) document.getElementById('account-panel')?.scrollIntoView({ block: 'nearest' });
  }, [accountOpen]);

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
    setAccountOpen(false);
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
    setAccountOpen(false);
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
      ? '后端 API 已连接'
      : connection === 'checking'
        ? '检查后端连接…'
        : '后端未连接';
  const milestones = [documentIds.length > 0, Boolean(job && !jobDirty), hasRealAnalysis];
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主内容
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32" fill="none">
              <path
                d="M5 24 13 8l5 9 9-13M5 24h11l5-9"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div>
            <strong>CareerPilot</strong>
            <span>求职准备工作台</span>
          </div>
        </div>
        <div className="workspace-label">
          准备工作区 <span>个人</span>
        </div>
        <nav aria-label="准备步骤" className="step-nav">
          {steps.map((label, index) => (
            <button
              key={label}
              className={step === index ? 'nav-item active' : 'nav-item'}
              aria-current={step === index ? 'page' : undefined}
              onClick={() => setStep(index)}
            >
              <Icon name={stepIcons[index]} />
              <span>{label}</span>
              {step === index && <span className="nav-indicator" aria-hidden="true" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <Icon name="shield" />
          <p>有依据，才有方向。</p>
          <span>你的经历由个人资料证明。公共知识只用于学习参考。</span>
        </div>
        <div className="connection-block">
          <StatusBadge
            label={connectionLabel}
            tone={connection === 'connected' ? 'success' : 'neutral'}
          />
          <details className="service-details">
            <summary>连接与开发工具</summary>
            <p>真实分析需后端、模型与开发依赖就绪。</p>
            <a href={api.docsUrl} target="_blank" rel="noreferrer">
              打开 API 文档 ↗
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
          </details>
        </div>
      </aside>

      <main id="main-content" className="main-content" tabIndex={-1}>
        <header className="topbar">
          <span>
            工作台 <span aria-hidden="true">/</span> {steps[step]}
          </span>
          <button
            type="button"
            className="account-button"
            aria-expanded={accountOpen}
            aria-controls="account-panel"
            onClick={() => setAccountOpen(!accountOpen)}
          >
            <span className="avatar">
              <Icon name="user" />
            </span>
            {user ? user.display_name : '登录账号'}
            <span aria-hidden="true">⌄</span>
          </button>
        </header>

        <section
          id="account-panel"
          hidden={!accountOpen && !authError}
          className="panel session-panel"
          aria-labelledby="session-title"
        >
          <div className="panel-title">
            <h2 id="session-title">{user ? '当前账号' : '登录你的工作区'}</h2>
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setAccountOpen(false);
                setAuthError('');
              }}
            >
              收起
            </button>
          </div>
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

        <div className="page-intro">
          <div className="intro-copy">
            <p className="intro-greeting">
              {user ? `${user.display_name}，开始今天的准备` : '每一步准备，都算数'}
            </p>
            <h1>{pageCopy[step][0]}</h1>
            <p>{pageCopy[step][1]}</p>
          </div>
          <div className="intro-art" aria-hidden="true">
            <span className="art-orbit" />
            <span className="art-sheet">
              <i />
              <i />
              <i />
              <b>✓</b>
            </span>
            <span className="art-compass">✧</span>
          </div>
        </div>
        {step === 0 && (
          <div className="preparation-path" aria-label="准备路径">
            {steps.slice(0, 4).map((label, index) => (
              <button
                type="button"
                key={label}
                onClick={() => setStep(index)}
                className={milestones[index] ? 'complete' : ''}
              >
                <span>{milestones[index] ? '✓' : String(index + 1).padStart(2, '0')}</span>
                <div>
                  <strong>{label}</strong>
                  <small>
                    {['收集你的经历', '保存目标 JD', '核对匹配证据', '确认行动安排'][index]}
                  </small>
                </div>
                {index < 3 && <Icon name="arrow" />}
              </button>
            ))}
          </div>
        )}

        {step === 0 && (
          <section aria-labelledby="documents-title">
            <div className="section-heading">
              <div>
                <h2 id="documents-title">你的资料</h2>
                <p className="muted small">准备简历与项目说明，开始一次有依据的分析。</p>
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
                <h3>添加一份经历</h3>
                <p className="muted">简历或项目说明，让你的能力有迹可循。</p>
                {!user && (
                  <p className="upload-login-hint">
                    上传前需要登录。
                    <button className="text-button" onClick={() => setAccountOpen(true)}>
                      登录账号
                    </button>
                  </p>
                )}
                <DocumentUpload
                  key={user?.id ?? 'anonymous'}
                  userId={user?.id}
                  onReadyChange={setDocumentIds}
                />
              </div>
              <div className="panel sample-panel">
                <span className="sample-label">先体验一下</span>
                <h3>从一份示例，看见整个流程。</h3>
                <p className="muted">
                  合成简历、校园项目和岗位 JD 已准备好。无需登录，也能核对分析与证据。
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
                  体验合成样例 <Icon name="arrow" />
                </button>
              </div>
            </div>
            <DocumentManager
              key={user?.id ?? 'anonymous'}
              userId={user?.id}
              onUpdated={(id) => {
                if (analysis?.document_versions.some((pin) => pin.document_id === id)) {
                  const stale = { ...analysis, stale: true };
                  setAnalysis(stale);
                  setAnalysisStale(true);
                  sessionStorage.setItem(ANALYSIS_KEY, JSON.stringify(stale));
                }
              }}
              onDeleted={(id) => {
                setDocumentIds((prev) => prev.filter((item) => item !== id));
                try {
                  const selected = JSON.parse(
                    sessionStorage.getItem('careerpilot.selected_documents') ?? '[]',
                  );
                  if (Array.isArray(selected)) {
                    sessionStorage.setItem(
                      'careerpilot.selected_documents',
                      JSON.stringify(selected.filter((item) => item !== id)),
                    );
                  }
                } catch {
                  // 本地选择状态损坏时忽略；刷新时仍会与已就绪资料求交集。
                }
              }}
            />
            <div className="footnote">
              原始资料、片段与结论分开保存。缺少证据时，系统应如实标记“资料未体现”。
            </div>
          </section>
        )}

        {step === 1 && (
          <section aria-labelledby="job-title">
            <div className="section-heading">
              <div>
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
                岗位或资料已更新，上次分析已过期；请重新分析后再生成计划。
              </p>
            )}
          </section>
        )}
        <div
          className="panel"
          hidden={
            step === 0 ||
            step === 3 ||
            step === 4 ||
            (step === 2 && (!hasRealAnalysis || analysisVisible))
          }
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
                setAnalysisStale(result?.stale ?? false);
                if (result) setAnalysisVisible(false);
              }}
              initialAnalysis={analysis}
              onGoToPlan={() => setStep(3)}
            />
          )}
        </div>
        {step === 3 && <PlanPanel analysisId={analysis?.id} analysisTitle={title} />}
        {step === 4 && <KnowledgeLibrary userId={user?.id} onLogin={() => setAccountOpen(true)} />}
        <footer className="page-footer">
          <span>CareerPilot · CS353FZ</span>
          <span>以经历为依据，以行动为下一步。</span>
        </footer>
      </main>
    </div>
  );
}
