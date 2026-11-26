import GlassBrand from '../components/GlassBrand';
import GlassSpotlight from '../components/GlassSpotlight';
import Icon from '../components/Icon';
import EffectsToggle from '../components/EffectsToggle';

export default function GlassHome({
  onStart,
  onSample,
  onKnowledge,
}: {
  onStart: () => void;
  onSample: () => void;
  onKnowledge: () => void;
}) {
  return (
    <div className="glass-home">
      <a className="skip-link" href="#glass-home-main">
        跳到主内容
      </a>
      <div className="glass-world" aria-hidden="true">
        <i />
        <i />
        <i />
        <span />
      </div>
      <header className="glass-home-header">
        <a className="glass-home-brand" href="/">
          <span>
            <GlassBrand />
          </span>
          CareerPilot
        </a>
        <nav aria-label="首页导航">
          <a href="#preparation-story">准备流程</a>
          <button onClick={onKnowledge}>学习素材</button>
        </nav>
        <EffectsToggle />
        <button className="glass-enter" onClick={onStart}>
          进入工作区
          <Icon name="arrow" />
        </button>
      </header>
      <main id="glass-home-main">
        <section className="glass-hero" aria-labelledby="glass-home-title">
          <div className="glass-hero-copy">
            <span className="glass-intro-line">
              <span />
              每一步准备，都有迹可循
            </span>
            <h1 id="glass-home-title">
              专注准备。
              <br />
              从容向前。
            </h1>
            <p>
              从真实经历出发，看见与目标的距离。
              <br />
              让每一次学习，都成为清晰的下一步。
            </p>
            <div className="glass-hero-actions">
              <button className="primary-button" onClick={onStart}>
                开始我的准备
                <Icon name="arrow" />
              </button>
              <button className="glass-sample-link" onClick={onSample}>
                <span aria-hidden="true">▷</span>体验完整示例
              </button>
            </div>
            <div className="glass-hero-footnote">
              <Icon name="shield" />
              <span>经历有原文可查，学习有来源可循。</span>
            </div>
          </div>
          <GlassSpotlight />
        </section>
        <section id="preparation-story" className="glass-process" aria-labelledby="process-title">
          <div className="glass-process-heading">
            <h2 id="process-title">把准备，变得清晰。</h2>
            <p>从一份经历，到下一步行动。</p>
          </div>
          <div className="glass-process-grid">
            <button onClick={onStart}>
              <span className="process-icon">
                <Icon name="documents" />
              </span>
              <span className="process-number">01</span>
              <h3>整理你的经历</h3>
              <p>添加简历与项目说明，保留属于你的原文。</p>
              <span className="process-action">
                准备资料
                <Icon name="arrow" />
              </span>
            </button>
            <button onClick={onSample}>
              <span className="process-icon">
                <Icon name="analysis" />
              </span>
              <span className="process-number">02</span>
              <h3>理解岗位差距</h3>
              <p>结论与证据放在一起，看见还需要补充什么。</p>
              <span className="process-action">
                查看示例
                <Icon name="arrow" />
              </span>
            </button>
            <button onClick={onKnowledge}>
              <span className="process-icon">
                <Icon name="knowledge" />
              </span>
              <span className="process-number">03</span>
              <h3>开始下一步实践</h3>
              <p>查阅学习来源，把准备转化成具体行动。</p>
              <span className="process-action">
                浏览素材
                <Icon name="arrow" />
              </span>
            </button>
          </div>
        </section>
      </main>
      <footer className="glass-home-footer">
        <span>CareerPilot</span>
        <EffectsToggle />
        <span>以经历为依据，以行动为下一步。</span>
      </footer>
    </div>
  );
}
