export default function SourceDetails({
  url,
  version,
  verifiedOn,
  scope,
  sourceId,
}: {
  url: string;
  version: string;
  verifiedOn?: string;
  scope?: string;
  sourceId?: string;
}) {
  let safeUrl: URL | null = null;
  try {
    const parsed = new URL(url);
    if (['https:', 'http:'].includes(parsed.protocol)) safeUrl = parsed;
  } catch {
    // An unavailable source is not presented as a working link.
  }
  return (
    <div className="source-disclosure">
      <details>
        <summary>
          <span>来源与版本</span>
          <small>{safeUrl?.hostname ?? '来源链接不可用'}</small>
        </summary>
        <dl className="source-meta">
          {sourceId && (
            <div>
              <dt>来源标识</dt>
              <dd>{sourceId}</dd>
            </div>
          )}
          <div>
            <dt>摘要版本</dt>
            <dd>{version}</dd>
          </div>
          {verifiedOn && (
            <div>
              <dt>来源核验日期</dt>
              <dd>{verifiedOn}</dd>
            </div>
          )}
          {scope && (
            <div>
              <dt>适用版本</dt>
              <dd>{scope}</dd>
            </div>
          )}
        </dl>
      </details>
      {safeUrl && (
        <a className="source-link" href={safeUrl.href} target="_blank" rel="noreferrer">
          阅读官方来源 <span aria-hidden="true">↗</span>
        </a>
      )}
    </div>
  );
}
