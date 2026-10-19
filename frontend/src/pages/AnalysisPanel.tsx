import { useState } from 'react';
import { sampleAnalysis, sampleEvidence } from '../api/fixtures';
import StatusBadge from '../components/StatusBadge';

const labels = {
  supported: { label: '有证据支持', tone: 'success' },
  partial: { label: '部分支持', tone: 'warning' },
  not_evidenced: { label: '资料未体现', tone: 'neutral' },
} as const;

export default function AnalysisPanel() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = sampleEvidence.find((item) => item.chunk_id === selectedId);
  return (
    <>
      <div className="section-heading">
        <div>
          <p className="eyebrow">03 / 匹配分析</p>
          <h2>每一条结论，都有出处</h2>
        </div>
        <StatusBadge label="合成契约样例" tone="warning" />
      </div>
      <p className="muted">
        以下为固定的开发样例，并非针对你上传资料生成的实际分析。未提供匹配百分比或录用预测。
      </p>
      <div className="result-layout">
        <div className="match-list">
          {sampleAnalysis.matches.map((match, index) => (
            <article className="match-card" key={match.requirement_id}>
              <div className="match-top">
                <span className="item-number">要求 {String(index + 1).padStart(2, '0')}</span>
                <StatusBadge {...labels[match.status]} />
              </div>
              <h3>{match.requirement}</h3>
              <p>{match.explanation}</p>
              {match.evidence_refs.length > 0 && (
                <div className="citation-list">
                  {match.evidence_refs.map((ref) => (
                    <button
                      key={ref.chunk_id}
                      className="citation"
                      aria-expanded={selectedId === ref.chunk_id}
                      aria-controls="evidence-panel"
                      onClick={() => setSelectedId(ref.chunk_id)}
                    >
                      <span aria-hidden="true">↗</span> 查看原文证据
                    </button>
                  ))}
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
        <aside id="evidence-panel" className="evidence-panel" aria-live="polite">
          {selected ? (
            <>
              <div className="panel-title">
                <h3>原文证据</h3>
                <button className="text-button" onClick={() => setSelectedId(null)}>
                  关闭
                </button>
              </div>
              <p className="source-name">{selected.filename}</p>
              <p className="muted small">
                版本 {selected.version} ·{' '}
                {selected.locator.kind === 'text'
                  ? `第 ${selected.locator.line_start}–${selected.locator.line_end} 行`
                  : `第 ${selected.locator.page} 页`}
              </p>
              <blockquote>{selected.quote}</blockquote>
              <p className="small muted">来源为仓库中的合成项目说明。引用位置由契约检查验证。</p>
            </>
          ) : (
            <>
              <div className="document-symbol" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6" />
                </svg>
              </div>
              <h3>把结论与原文放在一起</h3>
              <p className="muted">点击左侧“查看原文证据”，核对资料内容、版本与行号。</p>
              <div className="aside-note">
                “资料未体现”表示当前资料缺少证据，不等于用户不具备这项能力。
              </div>
            </>
          )}
        </aside>
      </div>
    </>
  );
}
