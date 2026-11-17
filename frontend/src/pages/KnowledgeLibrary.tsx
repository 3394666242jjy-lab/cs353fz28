import { useEffect, useRef, useState } from 'react';
import { filterKnowledge, knowledgeSources, roles, type Role } from '../api/knowledge';
import Icon from '../components/Icon';
import StatusBadge from '../components/StatusBadge';

export default function KnowledgeLibrary() {
  const [role, setRole] = useState<Role>('ai_product_manager');
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const detailRef = useRef<HTMLElement>(null);
  const sources = filterKnowledge(role, query, topic);
  const selected = sources.find((source) => source.id === selectedId);
  useEffect(() => {
    if (selected?.id && window.matchMedia('(max-width: 850px)').matches) {
      detailRef.current?.focus();
      detailRef.current?.scrollIntoView({ block: 'start' });
    }
  }, [selected?.id]);
  const topics = [
    ...new Set(
      knowledgeSources
        .filter((source) => source.roles.includes(role))
        .flatMap((source) => source.topics),
    ),
  ];

  return (
    <section className="knowledge-library" aria-labelledby="knowledge-title">
      <div className="section-heading">
        <div>
          <h2 id="knowledge-title">为下一个方向，补充知识</h2>
          <p className="muted">浏览项目整理的 13 份学习素材，按方向找到值得深入的主题。</p>
        </div>
        <StatusBadge label="本地素材预览" />
      </div>
      <div className="role-picker" role="group" aria-label="岗位方向">
        {(Object.keys(roles) as Role[]).map((key) => (
          <button
            key={key}
            className={`role-option ${role === key ? 'active' : ''}`}
            aria-pressed={role === key}
            onClick={() => {
              setRole(key);
              setTopic('');
              setSelectedId(null);
            }}
          >
            <span className="role-symbol" aria-hidden="true">
              {roles[key].symbol}
            </span>
            <strong>{roles[key].label}</strong>
            <span>{roles[key].description}</span>
            <small>
              {knowledgeSources.filter((source) => source.roles.includes(key)).length} 份素材
            </small>
          </button>
        ))}
      </div>
      <div className="reference-notice">
        <Icon name="shield" />
        <p>
          <strong>学习参考与个人经历分开</strong>
          <span>
            这里的内容是项目整理摘要，可帮助学习与理解；不能证明你具备某项技能。在线知识检索尚未接入。
          </span>
        </p>
      </div>
      <div className="knowledge-filters">
        <label>
          <span>搜索素材</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索标题、主题或摘要，如：评估"
          />
        </label>
        <label>
          <span>主题</span>
          <select value={topic} onChange={(event) => setTopic(event.target.value)}>
            <option value="">全部主题</option>
            {topics.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <p role="status">找到 {sources.length} 份素材</p>
      </div>
      <div className="knowledge-layout">
        <ul className="knowledge-list">
          {sources.map((source) => (
            <li key={source.id}>
              <button
                className={`knowledge-item ${selected?.id === source.id ? 'active' : ''}`}
                aria-expanded={selected?.id === source.id}
                aria-controls="knowledge-detail"
                onClick={() => setSelectedId(source.id)}
              >
                <span className="knowledge-item-top">
                  <span>整理摘要</span>
                  <span aria-hidden="true">↗</span>
                </span>
                <strong>{source.title}</strong>
                <span className="knowledge-topics">{source.topics.join(' / ')}</span>
                <small>{new URL(source.url).hostname}</small>
              </button>
            </li>
          ))}
          {!sources.length && (
            <li className="knowledge-empty">
              <h3>没有找到匹配的素材</h3>
              <p>试试更短的关键词，或清空关键词与主题。</p>
              <button
                className="secondary-button"
                onClick={() => {
                  setQuery('');
                  setTopic('');
                }}
              >
                清空筛选
              </button>
            </li>
          )}
        </ul>
        <article
          ref={detailRef}
          tabIndex={-1}
          id="knowledge-detail"
          className="knowledge-detail"
          aria-label="素材详情"
          aria-live="polite"
        >
          {selected ? (
            <>
              <div className="panel-title">
                <StatusBadge label="公共学习参考" tone="success" />
                <button className="text-button" onClick={() => setSelectedId(null)}>
                  关闭详情
                </button>
              </div>
              <h3>{selected.title}</h3>
              <p className="small muted">项目整理摘要，并非官方原文。</p>
              <div className="knowledge-content">
                {selected.content
                  .split(/\n\s*\n/)
                  .filter((block) => !block.startsWith('# '))
                  .map((block, index) =>
                    block.startsWith('## ') ? (
                      <h4 key={index}>{block.slice(3)}</h4>
                    ) : (
                      <p key={index}>{block}</p>
                    ),
                  )}
              </div>
              <dl className="source-meta">
                <div>
                  <dt>摘要版本</dt>
                  <dd>{selected.version}</dd>
                </div>
                <div>
                  <dt>来源核验日期</dt>
                  <dd>{selected.verified_on}</dd>
                </div>
                <div>
                  <dt>适用版本</dt>
                  <dd>{selected.version_scope}</dd>
                </div>
              </dl>
              <a className="source-link" href={selected.url} target="_blank" rel="noreferrer">
                阅读官方来源 <span aria-hidden="true">↗</span>
              </a>
              <p className="small muted">{new URL(selected.url).hostname}</p>
            </>
          ) : (
            <div className="knowledge-placeholder">
              <Icon name="knowledge" />
              <h3>从一个主题开始阅读</h3>
              <p>选择左侧素材，查看知识要点、适用边界与可追溯的官方来源。</p>
              <span>参考知识帮助你准备，个人经历仍需原文证据。</span>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
