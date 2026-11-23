import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import { describeError } from '../api/errors';
import { type Role } from '../api/knowledge';
import { LatestRequest } from '../api/latest-request';
import type { KnowledgeQueryResult, KnowledgeSource } from '../api/m3-types';
import Icon from '../components/Icon';
import StatusBadge from '../components/StatusBadge';
import RolePicker from '../components/RolePicker';
import SourceDetails from '../components/SourceDetails';

function knowledgeError(error: unknown): string {
  if (error instanceof ApiError && [404, 501].includes(error.status)) {
    return '当前服务尚未提供在线知识查询。请部署支持此功能的后端，或切换到本地素材预览。';
  }
  if (error instanceof DOMException && error.name === 'TimeoutError') {
    return '查询超时，请稍后重试。';
  }
  return describeError(error).message;
}

function sourceUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export default function OnlineKnowledgeLibrary({
  userId,
  onLogin,
}: {
  userId?: string;
  onLogin: () => void;
}) {
  const [role, setRole] = useState<Role>('ai_product_manager');
  return (
    <section className="knowledge-library" aria-labelledby="online-knowledge-title">
      <div className="section-heading">
        <div>
          <h2 id="online-knowledge-title">查询学习参考</h2>
          <p className="muted">查询当前服务已发布的公共知识，核对摘要与来源版本。</p>
        </div>
        <StatusBadge label="在线查询" />
      </div>
      <RolePicker value={role} onChange={setRole} label="在线查询岗位方向" />
      <div className="reference-notice">
        <Icon name="shield" />
        <p>
          <strong>公共知识只作学习参考</strong>
          <span>查询结果是项目整理摘要，不能作为你具备某项技能或经历的证据。</span>
        </p>
      </div>
      {userId ? (
        <KnowledgeResults key={role} role={role} />
      ) : (
        <div className="knowledge-empty online-state">
          <h3>登录后查询已发布的知识</h3>
          <p>使用工作区账号登录，即可读取在线来源与查询参考；也可以继续浏览本地素材。</p>
          <button className="primary-button" onClick={onLogin}>
            登录账号
          </button>
        </div>
      )}
    </section>
  );
}

function KnowledgeResults({ role }: { role: Role }) {
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [catalogState, setCatalogState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [catalogError, setCatalogError] = useState('');
  const [reload, setReload] = useState(0);
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState('');
  const [version, setVersion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [requestId, setRequestId] = useState('');
  const [result, setResult] = useState<KnowledgeQueryResult | null>(null);
  const [question, setQuestion] = useState('');
  const [cancelled, setCancelled] = useState(false);
  const catalogRequest = useRef(new LatestRequest());
  const queryRequest = useRef(new LatestRequest());

  useEffect(() => {
    const ticket = catalogRequest.current.start();
    setCatalogState('loading');
    setCatalogError('');
    setSources([]);
    void api
      .listKnowledgeSources(role, ticket.signal)
      .then(({ data }) => {
        if (!ticket.isCurrent()) return;
        setSources(data.items);
        setCatalogState('ready');
      })
      .catch((err: unknown) => {
        if (!ticket.isCurrent()) return;
        setCatalogError(knowledgeError(err));
        setCatalogState('error');
      });
    return () => catalogRequest.current.cancel();
  }, [role, reload]);

  useEffect(() => {
    const request = queryRequest.current;
    return () => request.cancel();
  }, []);

  function resetQuery() {
    queryRequest.current.cancel();
    setBusy(false);
    setResult(null);
    setError('');
    setRequestId('');
    setCancelled(false);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim() || catalogState !== 'ready' || !sources.length) return;
    const ticket = queryRequest.current.start();
    setBusy(true);
    setResult(null);
    setError('');
    setRequestId('');
    setCancelled(false);
    setQuestion(query.trim());
    try {
      const response = await api.queryKnowledge(
        {
          role,
          query: query.trim(),
          top_k: 4,
          max_chars: 6000,
          topics: topic ? [topic] : [],
          source_version: version || null,
        },
        ticket.signal,
      );
      if (!ticket.isCurrent()) return;
      if (
        response.data.items.some(
          (item) =>
            item.usage !== 'reference_only' ||
            item.content_kind !== 'editorial_summary' ||
            !item.roles.includes(role),
        )
      ) {
        throw new Error('服务返回的参考用途或岗位范围不兼容，请联系维护者核对接口。');
      }
      setResult(response.data);
      setRequestId(response.request_id);
    } catch (err) {
      if (!ticket.isCurrent()) return;
      setError(knowledgeError(err));
      if (err instanceof ApiError) setRequestId(err.requestId ?? '');
    } finally {
      if (ticket.isCurrent()) setBusy(false);
    }
  }

  const topics = [...new Set(sources.flatMap((source) => source.topics))];
  const versions = [...new Set(sources.map((source) => source.version))];
  return (
    <div className="online-knowledge">
      {catalogState === 'loading' && (
        <p role="status" className="online-state">
          正在读取已发布的来源…
        </p>
      )}
      {catalogState === 'error' && (
        <div className="online-state">
          <p className="error-box" role="alert">
            {catalogError}
          </p>
          <button className="secondary-button" onClick={() => setReload((value) => value + 1)}>
            重试读取来源
          </button>
        </div>
      )}
      {catalogState === 'ready' && sources.length === 0 && (
        <div className="knowledge-empty online-state">
          <h3>这个方向还没有已发布的来源</h3>
          <p>可以切换岗位方向，或在维护者发布知识后重新读取。</p>
          <button className="secondary-button" onClick={() => setReload((value) => value + 1)}>
            重新读取来源
          </button>
        </div>
      )}
      {catalogState === 'ready' && sources.length > 0 && (
        <>
          <form className="knowledge-query-form" onSubmit={(event) => void submit(event)}>
            <label htmlFor="knowledge-question">想了解什么？</label>
            <textarea
              id="knowledge-question"
              required
              maxLength={2000}
              rows={3}
              value={query}
              onChange={(event) => {
                resetQuery();
                setQuery(event.target.value);
              }}
              placeholder="例如：如何为 Agent 设计可恢复的工作流？"
            />
            <div className="knowledge-query-filters">
              <label>
                主题
                <select
                  value={topic}
                  onChange={(event) => {
                    resetQuery();
                    setTopic(event.target.value);
                  }}
                >
                  <option value="">全部主题</option>
                  {topics.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
              <label>
                来源版本
                <select
                  value={version}
                  onChange={(event) => {
                    resetQuery();
                    setVersion(event.target.value);
                  }}
                >
                  <option value="">全部已发布版本</option>
                  {versions.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
              <div className="knowledge-query-actions">
                <button type="submit" className="primary-button" disabled={busy || !query.trim()}>
                  {busy ? '查询中…' : '查询学习参考'}
                </button>
                {busy && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                      resetQuery();
                      setCancelled(true);
                    }}
                  >
                    取消查询
                  </button>
                )}
              </div>
            </div>
            <p className="small muted">每次最多返回 4 段参考。问题 {query.length} / 2000 字。</p>
          </form>
          <div className="online-results" aria-busy={busy}>
            <p role="status">
              {busy
                ? '正在检索公共知识…'
                : cancelled
                  ? '已取消查询，可以修改问题后重新查询。'
                  : result
                    ? `找到 ${result.items.length} 段学习参考`
                    : '填写问题后查询，结果会显示在这里。'}
            </p>
            {error && (
              <div>
                <p className="error-box" role="alert">
                  {error}
                </p>
                <p className="small muted">可保留当前问题，点击“查询学习参考”重试。</p>
              </div>
            )}
            {result && (
              <>
                <h3>关于“{question}”</h3>
                {result.notice && <p className="small muted">{result.notice}</p>}
                {result.items.length === 0 ? (
                  <div className="knowledge-empty">
                    <h4>没有找到相关的学习参考</h4>
                    <p>试试更具体的问题，或选择全部主题与版本。无匹配时不会生成来源。</p>
                  </div>
                ) : (
                  <ul className="online-reference-list">
                    {result.items.map((item, index) => (
                      <li key={`${item.source_id}:${item.source_version}:${index}`}>
                        <StatusBadge label="公共学习参考" tone="success" />
                        <h4>{item.title}</h4>
                        <div className="online-summary">
                          {item.summary
                            .split(/\n\s*\n/)
                            .filter((block) => !block.startsWith('# '))
                            .map((block, blockIndex) =>
                              block.startsWith('## ') ? (
                                <h5 key={blockIndex}>{block.slice(3)}</h5>
                              ) : (
                                <p key={blockIndex}>{block}</p>
                              ),
                            )}
                        </div>
                        <SourceDetails
                          url={item.source_url}
                          version={item.source_version}
                          sourceId={item.source_id}
                        />
                        <p className="small muted">项目整理摘要，并非官方原文；仅供学习参考。</p>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
            {requestId && (
              <details className="knowledge-request">
                <summary>查询诊断信息</summary>
                <p className="small muted">请求标识：{requestId}</p>
              </details>
            )}
          </div>
          <details className="online-catalog">
            <summary>查看 {sources.length} 份已发布来源</summary>
            <ul>
              {sources.map((source) => (
                <li key={`${source.id}:${source.version}`}>
                  <strong>{source.title}</strong>
                  <span>
                    版本 {source.version}；核验日期 {source.verified_on}
                  </span>
                  <span>{source.topics.join(' / ')}</span>
                  {sourceUrl(source.url) && (
                    <a href={sourceUrl(source.url)} target="_blank" rel="noreferrer">
                      阅读官方来源
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </div>
  );
}
