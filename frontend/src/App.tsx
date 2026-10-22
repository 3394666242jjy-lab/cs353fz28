import { useEffect, useState, type ChangeEvent } from 'react';
import { api } from './api/client';
import { sampleInputs } from './api/fixtures';
import StatusBadge from './components/StatusBadge';
import AnalysisPanel from './pages/AnalysisPanel';

const steps = ['资料准备', '目标岗位', '匹配分析'];
type Connection = 'checking' | 'connected' | 'disconnected';

export default function App() {
  const [step, setStep] = useState(0);
  const [connection, setConnection] = useState<Connection>('checking');
  const [dependencies, setDependencies] = useState<Record<string, string> | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeMessage, setProbeMessage] = useState('');
  const [sampleLoaded, setSampleLoaded] = useState(false);
  const [analysisVisible, setAnalysisVisible] = useState(false);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [title, setTitle] = useState('后端开发实习生');
  const [jd, setJd] = useState('');

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
    setSelectedFile(null);
    setTitle('后端开发实习生');
    setJd(sampleInputs.jdText);
    setStep(1);
  }

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    setSelectedFile(event.target.files?.[0]?.name ?? null);
    setSampleLoaded(false);
    setAnalysisVisible(false);
    event.target.value = '';
  }

  function editJd(value: string) {
    setJd(value);
    setSampleLoaded(false);
    setAnalysisVisible(false);
  }

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
          <p className="eyebrow">M1 / 开发骨架</p>
          <p>先整理资料，再用证据理解岗位要求。</p>
          <span>计划与面试功能将在后续阶段接入。</span>
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
          <StatusBadge label="本地会话 · 未登录" />
        </header>
        <div className="page-intro">
          <p className="eyebrow">CAREERPILOT / WORKSPACE</p>
          <h1>你的求职准备，从经历开始。</h1>
          <p>整理个人资料，选择目标岗位，再查看每条要求对应的证据。</p>
        </div>
        <div className="demo-notice">
          <span className="notice-label">样例模式</span>
          <p>
            这里使用合成资料展示交互。选择文件不会上传，也不会调用模型；真实上传、登录和分析接口尚未实现。
          </p>
        </div>

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
                  M1 将支持有文本层 PDF / Markdown / TXT，最大 10
                  MiB。当前仅选择文件，上传由后端模块接入。
                </p>
                <label htmlFor="local-file" className="field-label">
                  本地文件预选
                </label>
                <input id="local-file" type="file" accept=".pdf,.md,.txt" onChange={chooseFile} />
                <p className="small" role="status">
                  {selectedFile
                    ? `已选择 ${selectedFile}，尚未上传。`
                    : '未选择文件；本页不会保存你的个人资料。'}
                </p>
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
                  : '自定义内容的真实分析尚未接入；载入示例可预览完整交互。'}
              </p>
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
          ) : (
            <section className="panel empty-result">
              <p className="eyebrow">03 / 匹配分析</p>
              <h2>从一份资料和一个岗位开始</h2>
              <p className="muted">先载入合成资料和示例 JD，查看带原文证据的分析页面。</p>
              <button className="primary-button" onClick={loadSample}>
                载入合成样例
              </button>
            </section>
          ))}
        <footer className="page-footer">
          <span>CareerPilot · CS353FZ</span>
          <span>开发预览 / 合成数据</span>
        </footer>
      </main>
    </div>
  );
}
