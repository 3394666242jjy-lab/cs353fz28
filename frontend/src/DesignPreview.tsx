import { useRef, useState } from 'react';
import { sampleAnalysis, sampleEvidence, sampleInputs } from './api/fixtures';
import Icon from './components/Icon';
import './design-preview.css';

export const designDirections = {
  aurora: { code: 'A', name: '极光流线', detail: '紫色光晕 · 悬浮工作区 · 边缘流光' },
  orbital: { code: 'B', name: '轨道光场', detail: '蓝色光环 · SVG 轨道 · 空间层次' },
  glass: { code: 'C', name: '午夜玻璃', detail: '烟灰玻璃 · 银白折射 · 克制留白' },
} as const;
export type DesignDirection = keyof typeof designDirections;
type View = 'materials' | 'analysis' | 'plan' | 'knowledge';
const resumeExcerpt = sampleInputs.resumeText
  .split(/\r?\n/)
  .find((line) => line.startsWith('校园设备借用系统'));

const views = [
  { key: 'materials', label: '我的资料', icon: 'documents' },
  { key: 'analysis', label: '匹配分析', icon: 'analysis' },
  { key: 'plan', label: '准备计划', icon: 'plan' },
  { key: 'knowledge', label: '学习素材', icon: 'knowledge' },
] as const;

function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <path
        d="M5 24 13 8l5 9 9-13M5 24h11l5-9"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function OrbitalScene() {
  return (
    <div className="concept-orbit" aria-hidden="true">
      <div className="orbit-aura" />
      <svg className="orbit-map" viewBox="0 0 600 500" fill="none">
        <defs>
          <linearGradient
            id="orbit-line"
            x1="80"
            y1="80"
            x2="520"
            y2="420"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="#78d9ff" stopOpacity=".9" />
            <stop offset=".45" stopColor="#778fff" stopOpacity=".12" />
            <stop offset="1" stopColor="#b3a0ff" stopOpacity=".75" />
          </linearGradient>
          <radialGradient id="orbit-disc">
            <stop stopColor="#63cfff" stopOpacity=".16" />
            <stop offset="1" stopColor="#63cfff" stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle cx="300" cy="250" r="215" fill="url(#orbit-disc)" />
        <ellipse
          cx="300"
          cy="250"
          rx="235"
          ry="96"
          transform="rotate(-25 300 250)"
          stroke="url(#orbit-line)"
        />
        <ellipse
          cx="300"
          cy="250"
          rx="218"
          ry="120"
          transform="rotate(36 300 250)"
          stroke="url(#orbit-line)"
        />
        <circle cx="300" cy="250" r="163" stroke="url(#orbit-line)" strokeWidth=".7" />
        <circle
          cx="300"
          cy="250"
          r="215"
          stroke="#73bafa"
          strokeOpacity=".1"
          strokeDasharray="2 8"
        />
        <g className="orbit-particle">
          <circle cx="463" cy="250" r="3" fill="#c1eeff" />
          <circle cx="463" cy="250" r="10" fill="#6ccfff" opacity=".12" />
        </g>
        <path
          d="M182 157 300 250 437 175M300 250 226 379"
          stroke="#a8dcff"
          strokeOpacity=".18"
          strokeDasharray="3 6"
        />
      </svg>
      <div className="orbit-core">
        <BrandMark />
      </div>
      <div className="orbit-node node-document">
        <Icon name="documents" />
        <span>
          经历资料<small>你的准备起点</small>
        </span>
      </div>
      <div className="orbit-node node-job">
        <Icon name="job" />
        <span>
          目标岗位<small>明确技能要求</small>
        </span>
      </div>
      <div className="orbit-node node-evidence">
        <Icon name="shield" />
        <span>
          原文证据<small>让结论有出处</small>
        </span>
      </div>
    </div>
  );
}

function WorkspacePreview({
  view,
  onView,
  focused,
}: {
  view: View;
  onView: (next: View) => void;
  focused: boolean;
}) {
  const [selected, setSelected] = useState(0);
  const [confirmed, setConfirmed] = useState(false);
  const labels = ['有证据支持', '部分支持', '资料未体现'];
  return (
    <div className={`concept-workspace ${focused ? 'workspace-focused' : ''}`}>
      <div className="workspace-windowbar">
        <div className="window-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </div>
        <span>CareerPilot / 准备工作区</span>
        <span className="concept-demo-tag">合成示例</span>
      </div>
      <div className="workspace-content">
        <nav className="workspace-nav" aria-label="演示工作区">
          <div className="workspace-identity">
            <span>
              <BrandMark />
            </span>
            我的准备
          </div>
          {views.map((item) => (
            <button
              key={item.key}
              aria-pressed={view === item.key}
              onClick={() => onView(item.key)}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
            </button>
          ))}
          <p>
            <Icon name="shield" /> 个人资料仅用于你的准备
          </p>
        </nav>
        <div className="workspace-main">
          <div className="workspace-title">
            <h2>{views.find((item) => item.key === view)?.label}</h2>
            <span>
              {view === 'materials'
                ? '2 份示例资料'
                : view === 'analysis'
                  ? '后端开发实习生'
                  : '准备工作区'}
            </span>
          </div>
          {view === 'materials' && (
            <div className="workspace-materials">
              <div className="material-list">
                <p>从经历出发，让每一步都有依据。</p>
                {['resume-demo.md', 'project-demo.md'].map((name, index) => (
                  <button
                    key={name}
                    className={`material-row ${selected === index ? 'selected' : ''}`}
                    onClick={() => setSelected(index)}
                    aria-pressed={selected === index}
                  >
                    <span className="material-icon">
                      <Icon name={index ? 'job' : 'documents'} />
                    </span>
                    <span>
                      <strong>{name}</strong>
                      <small>{index ? '项目经历与实现过程' : '教育背景与个人经历'}</small>
                    </span>
                    <span className="material-ready">
                      <i />
                      已就绪
                    </span>
                  </button>
                ))}
              </div>
              <aside className="workspace-excerpt">
                <span>
                  <Icon name="shield" /> 原文摘录
                </span>
                <h3>{selected === 0 ? '从你的经历开始' : '每一项能力，都有出处'}</h3>
                <p>{selected === 0 ? resumeExcerpt : sampleEvidence[0]?.quote}</p>
                <small>{selected === 0 ? 'resume-demo.md' : 'project-demo.md'} · 示例版本 1</small>
              </aside>
            </div>
          )}
          {view === 'analysis' && (
            <div className="workspace-analysis">
              {sampleAnalysis.matches.map((item, index) => (
                <div key={item.requirement} className="concept-match">
                  <span className={`concept-match-status match-${index}`}>
                    <i />
                    {labels[index]}
                  </span>
                  <strong>{item.requirement}</strong>
                  <p>{item.explanation}</p>
                </div>
              ))}
              <p className="workspace-footnote">示例分析只展示资料支持范围，不预测录用结果。</p>
            </div>
          )}
          {view === 'plan' && (
            <div className="workspace-plan">
              <p>把需要补充的内容，变成下一步行动。</p>
              {[
                '整理数据库设计的原文证据',
                '补充性能优化的测量记录',
                '实践一次 Docker 镜像部署',
              ].map((task, index) => (
                <div className="concept-task" key={task}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <strong>{task}</strong>
                  <small>学习与实践</small>
                </div>
              ))}
              <button className="workspace-next" onClick={() => setConfirmed(!confirmed)}>
                {confirmed ? '已确认示例计划' : '确认示例计划'}
                <Icon name="arrow" />
              </button>
              {confirmed && (
                <p role="status" className="workspace-footnote">
                  示例确认完成，未创建后端任务。
                </p>
              )}
            </div>
          )}
          {view === 'knowledge' && (
            <div className="workspace-plan">
              <p>公共知识只作学习参考，个人能力仍需经历证明。</p>
              {[
                'React 组件拆分与最小状态',
                'Fetch 的 HTTP 错误与取消',
                '暂停、恢复与副作用幂等',
              ].map((title) => (
                <div className="concept-task" key={title}>
                  <Icon name="knowledge" />
                  <strong>{title}</strong>
                  <small>学习参考</small>
                </div>
              ))}
              <a className="workspace-next" href="/">
                在工作台浏览来源 <Icon name="arrow" />
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function DesignPreview({ initialDirection }: { initialDirection: DesignDirection }) {
  const [direction, setDirection] = useState(initialDirection);
  const [view, setView] = useState<View>('materials');
  const [focused, setFocused] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  function choose(next: DesignDirection) {
    setDirection(next);
    window.history.replaceState(null, '', `?design=${next}`);
    setFocused(false);
  }
  function explore(next: View) {
    setView(next);
    setFocused(true);
    stageRef.current?.scrollIntoView({
      block: 'center',
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
        ? 'instant'
        : 'smooth',
    });
  }
  return (
    <div className={`design-preview concept--${direction}`}>
      <div className="concept-reviewbar">
        <span>
          <strong>风格探索</strong>
          <small>先选方向，再打磨细节</small>
        </span>
        <div className="concept-directions" role="group" aria-label="选择设计方向">
          {(Object.keys(designDirections) as DesignDirection[]).map((key) => (
            <button key={key} aria-pressed={key === direction} onClick={() => choose(key)}>
              <b>{designDirections[key].code}</b>
              {designDirections[key].name}
            </button>
          ))}
        </div>
        <span className="concept-review-status">
          <i />
          本地演示，尚未上传
        </span>
      </div>
      <div className="concept-ambient" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
      <div className="concept-grain" aria-hidden="true" />
      <header className="concept-header">
        <a href="/" className="concept-brand">
          <span>
            <BrandMark />
          </span>
          CareerPilot
        </a>
        <span className="concept-product-label">让准备更有方向</span>
        <a className="concept-open" href="/">
          打开工作台 <Icon name="arrow" />
        </a>
      </header>
      <main className="concept-body">
        <section className="concept-intro" aria-labelledby="concept-title">
          <div className="concept-announcement">
            <span />
            从经历到机会，步步有据
          </div>
          <h1 id="concept-title">
            {direction === 'glass' ? (
              <>
                专注准备。
                <br />
                从容向前。
              </>
            ) : direction === 'orbital' ? (
              <>
                让每一份经历，
                <br />
                找到它的方向。
              </>
            ) : (
              <>
                你的下一次机会，
                <br />
                从此刻开始准备。
              </>
            )}
          </h1>
          <p>
            整理真实经历，理解岗位差距。
            <br />
            让每一次学习，都通向清晰的下一步。
          </p>
          <div className="concept-actions">
            <button className="concept-primary" onClick={() => explore('materials')}>
              开始准备 <Icon name="arrow" />
            </button>
            <button className="concept-secondary" onClick={() => explore('analysis')}>
              <span className="concept-play" aria-hidden="true">
                ▷
              </span>
              体验示例
            </button>
          </div>
          {direction === 'glass' && (
            <div className="glass-principle">
              <Icon name="shield" />
              <p>
                经历有证据。
                <br />
                <span>学习有来源。</span>
              </p>
            </div>
          )}
        </section>
        {direction === 'orbital' && <OrbitalScene />}
        <div className="concept-stage" ref={stageRef} id="concept-workspace">
          {direction === 'aurora' && <div className="concept-horizon" aria-hidden="true" />}
          {direction === 'glass' && (
            <div className="glass-lens" aria-hidden="true">
              <i />
              <i />
            </div>
          )}
          <WorkspacePreview view={view} onView={setView} focused={focused} />
        </div>
      </main>
      <footer className="concept-flow" aria-label="求职准备流程">
        {[
          ['documents', '整理经历', '从简历与项目开始'],
          ['job', '明确目标', '对照岗位要求'],
          ['analysis', '看见差距', '每条结论可追溯'],
          ['plan', '开始行动', '安排下一步实践'],
        ].map(([icon, title, description], index) => (
          <div key={title}>
            <span className="flow-mark">
              <Icon name={icon as 'documents' | 'job' | 'analysis' | 'plan'} />
            </span>
            <span>
              <strong>{title}</strong>
              <small>{description}</small>
            </span>
            {index < 3 && <span className="flow-connector" aria-hidden="true" />}
          </div>
        ))}
      </footer>
    </div>
  );
}
