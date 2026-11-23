import { useEffect, useRef, useState } from 'react';
import { filterKnowledge, knowledgeSources, type Role } from '../api/knowledge';
import Icon from '../components/Icon';
import RolePicker from '../components/RolePicker';
import StatusBadge from '../components/StatusBadge';
import ReadingDialog from '../components/ReadingDialog';
import SourceDetails from '../components/SourceDetails';
import { highlightParts, parseReadingState } from '../components/reading-state';
import OnlineKnowledgeLibrary from './OnlineKnowledgeLibrary';

export default function KnowledgeLibrary({
  userId,
  onLogin,
}: {
  userId?: string;
  onLogin: () => void;
}) {
  const [mode, setMode] = useState<'local' | 'online'>('local');
  return (
    <div>
      <div className="knowledge-mode" role="group" aria-label="知识素材模式">
        <button aria-pressed={mode === 'local'} onClick={() => setMode('local')}>
          本地素材预览
        </button>
        <button aria-pressed={mode === 'online'} onClick={() => setMode('online')}>
          在线知识查询
        </button>
      </div>
      {mode === 'local' ? (
        <LocalKnowledgeLibrary key={userId ?? 'anonymous'} owner={userId ?? 'anonymous'} />
      ) : (
        <OnlineKnowledgeLibrary key={userId ?? 'anonymous'} userId={userId} onLogin={onLogin} />
      )}
    </div>
  );
}

function Highlight({ text, query }: { text: string; query: string }) {
  return highlightParts(text, query).map((part, index) =>
    part.match ? <mark key={index}>{part.text}</mark> : part.text,
  );
}
type KnowledgeSource = (typeof knowledgeSources)[number];
function KnowledgeBody({
  source,
  query,
  focused = false,
}: {
  source: KnowledgeSource;
  query: string;
  focused?: boolean;
}) {
  return (
    <>
      {focused ? <h2>{source.title}</h2> : <h3>{source.title}</h3>}
      <p className="small muted">项目整理摘要，并非官方原文。</p>
      <div className="knowledge-content">
        {source.content
          .split(/\n\s*\n/)
          .filter((block) => !block.startsWith('# '))
          .map((block, index) =>
            block.startsWith('## ') ? (
              focused ? (
                <h3 key={index}>{block.slice(3)}</h3>
              ) : (
                <h4 key={index}>{block.slice(3)}</h4>
              )
            ) : (
              <p key={index}>
                <Highlight text={block} query={query} />
              </p>
            ),
          )}
      </div>
      <SourceDetails
        url={source.url}
        version={source.version}
        verifiedOn={source.verified_on}
        scope={source.version_scope}
      />
    </>
  );
}

function LocalKnowledgeLibrary({ owner }: { owner: string }) {
  const storageKey = 'careerpilot.reading:v1:' + owner;
  const [saved] = useState(() => {
    try {
      return parseReadingState(sessionStorage.getItem(storageKey));
    } catch {
      return parseReadingState(null);
    }
  });
  const [role, setRole] = useState<Role>(saved.role);
  const [query, setQuery] = useState(saved.query);
  const [topic, setTopic] = useState(saved.topic);
  const [selectedId, setSelectedId] = useState<string | null>(saved.selectedId);
  const [focused, setFocused] = useState(false);
  const [userSelected, setUserSelected] = useState(false);
  const detailRef = useRef<HTMLElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const focusTrigger = useRef<HTMLButtonElement>(null);
  const current = useRef({ role, query, topic, selectedId });
  current.current = { role, query, topic, selectedId };
  const scrollPoint = useRef({ pageY: saved.pageY, listY: saved.listY });
  const sources = filterKnowledge(role, query, topic);
  const selected = sources.find((source) => source.id === selectedId);
  useEffect(() => {
    if (userSelected && selected?.id && window.matchMedia('(max-width: 850px)').matches) {
      detailRef.current?.focus();
      detailRef.current?.scrollIntoView({ block: 'start' });
    }
  }, [selected?.id, userSelected]);
  useEffect(() => {
    let leaving = false;
    const persist = () => {
      try {
        sessionStorage.setItem(
          storageKey,
          JSON.stringify({ version: 1, ...current.current, ...scrollPoint.current }),
        );
      } catch {
        /* Optional storage. */
      }
    };
    const record = () => {
      if (leaving) return;
      // Returning to the navigation must not replace the last visible reading position.
      if (current.current.selectedId && detailRef.current) {
        const detail = detailRef.current.getBoundingClientRect();
        if (detail.top >= window.innerHeight || detail.bottom <= 0) return;
      }
      scrollPoint.current = { pageY: window.scrollY, listY: listRef.current?.scrollTop ?? 0 };
    };
    const beforeNavigate = () => {
      record();
      leaving = true;
      persist();
    };
    const frame = requestAnimationFrame(() => {
      if (listRef.current) listRef.current.scrollTop = saved.listY;
      if (saved.pageY) window.scrollTo({ top: saved.pageY, behavior: 'instant' });
    });
    const list = listRef.current;
    window.addEventListener('scroll', record, { passive: true });
    list?.addEventListener('scroll', record, { passive: true });
    window.addEventListener('pagehide', persist);
    window.addEventListener('careerpilot-before-navigate', beforeNavigate);
    return () => {
      cancelAnimationFrame(frame);
      persist();
      window.removeEventListener('scroll', record);
      list?.removeEventListener('scroll', record);
      window.removeEventListener('pagehide', persist);
      window.removeEventListener('careerpilot-before-navigate', beforeNavigate);
    };
  }, [storageKey, saved]);
  useEffect(() => {
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify({ version: 1, ...current.current, ...scrollPoint.current }),
      );
    } catch {
      /* Optional storage. */
    }
  }, [role, query, topic, selectedId, storageKey]);
  function clearFilters() {
    setQuery('');
    setTopic('');
  }
  function returnToList() {
    setUserSelected(false);
    listRef.current
      ?.querySelector<HTMLButtonElement>('[aria-expanded="true"]')
      ?.focus({ preventScroll: true });
    listRef.current?.scrollIntoView({ block: 'nearest' });
  }
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
          <h2 id="knowledge-title">学习资料库</h2>
          <p className="muted">浏览项目整理的 13 份学习素材，按方向找到值得深入的主题。</p>
        </div>
        <StatusBadge label="本地素材预览" />
      </div>
      <RolePicker
        value={role}
        counts={{
          ai_product_manager: knowledgeSources.filter((source) =>
            source.roles.includes('ai_product_manager'),
          ).length,
          fullstack: knowledgeSources.filter((source) => source.roles.includes('fullstack')).length,
          agent_engineer: knowledgeSources.filter((source) =>
            source.roles.includes('agent_engineer'),
          ).length,
        }}
        onChange={(key) => {
          setRole(key);
          setTopic('');
          setSelectedId(null);
          setUserSelected(false);
        }}
      />
      <div className="reference-notice">
        <Icon name="shield" />
        <p>
          <strong>学习参考与个人经历分开</strong>
          <span>
            这里的内容是项目整理摘要，可帮助学习与理解；不能证明你具备某项技能。本地预览不访问在线知识库。
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
      {(query.trim() || topic) && (
        <div className="filter-chips" aria-label="当前筛选">
          {query
            .trim()
            .split(/\s+/)
            .filter(Boolean)
            .map((term, index) => (
              <button
                type="button"
                key={index}
                aria-label={'移除关键词 ' + term}
                onClick={() =>
                  setQuery(
                    query
                      .trim()
                      .split(/\s+/)
                      .filter((_, i) => i !== index)
                      .join(' '),
                  )
                }
              >
                关键词：{term}
                <span aria-hidden="true">×</span>
              </button>
            ))}
          {topic && (
            <button type="button" aria-label={'移除主题 ' + topic} onClick={() => setTopic('')}>
              主题：{topic}
              <span aria-hidden="true">×</span>
            </button>
          )}
          <button type="button" className="clear-filters" onClick={clearFilters}>
            清空筛选
          </button>
        </div>
      )}
      <div className={'knowledge-layout ' + (selected ? 'has-selection' : '')}>
        <ul ref={listRef} className="knowledge-list" aria-label="学习素材列表">
          {sources.map((source) => (
            <li key={source.id}>
              <button
                className={'knowledge-item ' + (selected?.id === source.id ? 'active' : '')}
                aria-expanded={selected?.id === source.id}
                aria-controls="knowledge-detail"
                onClick={() => {
                  setUserSelected(true);
                  setSelectedId(source.id);
                }}
              >
                <span className="knowledge-item-top">
                  <span>整理摘要</span>
                  <span aria-hidden="true">↗</span>
                </span>
                <strong>
                  <Highlight text={source.title} query={query} />
                </strong>
                <span className="knowledge-topics">
                  <Highlight text={source.topics.join(' / ')} query={query} />
                </span>
                <small>{new URL(source.url).hostname}</small>
              </button>
            </li>
          ))}
          {!sources.length && (
            <li className="knowledge-empty">
              <h3>没有找到匹配的素材</h3>
              <p>试试更短的关键词，或清空关键词与主题。</p>
              <button className="secondary-button" onClick={clearFilters}>
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
              <div className="panel-title knowledge-detail-actions">
                <StatusBadge label="公共学习参考" tone="success" />
                <button
                  ref={focusTrigger}
                  type="button"
                  className="secondary-button focus-reading-button"
                  onClick={() => setFocused(true)}
                >
                  专注阅读
                </button>
                <button
                  className="text-button"
                  onClick={() => {
                    returnToList();
                    setSelectedId(null);
                  }}
                >
                  关闭详情
                </button>
              </div>
              <div className="knowledge-detail-copy" key={selected.id}>
                <KnowledgeBody source={selected} query={query} />
              </div>
              <button
                type="button"
                className="text-button return-to-materials"
                onClick={returnToList}
              >
                返回素材列表
              </button>
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
      {focused && selected && (
        <ReadingDialog
          title={selected.title}
          trigger={focusTrigger}
          onClose={() => setFocused(false)}
        >
          <StatusBadge label="公共学习参考" tone="success" />
          <KnowledgeBody source={selected} query={query} focused />
        </ReadingDialog>
      )}
    </section>
  );
}
