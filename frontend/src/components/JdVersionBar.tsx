import StatusBadge from './StatusBadge';
import { useEffect, useState } from 'react';

/** 当前岗位在本地记录的版本信息；sample 表示只保存在本地的演示岗位。 */
export interface JobVersionInfo {
  id: string;
  version: number;
  title: string;
  raw_jd: string;
  sample?: boolean;
}

export interface JobNotice {
  tone: 'success' | 'warning' | 'danger';
  text: string;
}

interface JdVersionBarProps {
  job: JobVersionInfo | null;
  sampleMode?: boolean;
  canSave?: boolean;
  dirty: boolean;
  busy: boolean;
  notice: JobNotice | null;
  stale: boolean;
  onSave: () => void;
  onSampleSave: () => void;
}

export default function JdVersionBar({
  job,
  sampleMode = false,
  canSave = true,
  dirty,
  busy,
  notice,
  stale,
  onSave,
  onSampleSave,
}: JdVersionBarProps) {
  const saveDisabled = !canSave || busy || (job !== null && !job.sample && !dirty);
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    if (notice?.tone !== 'success') {
      setConfirmed(false);
      return;
    }
    setConfirmed(true);
    const timer = window.setTimeout(() => setConfirmed(false), 1800);
    return () => window.clearTimeout(timer);
  }, [notice]);
  return (
    <div className="jd-version-bar">
      <div className="jd-version-head">
        <div className="jd-version-meta">
          <StatusBadge
            label={job ? `v${job.version}${job.sample ? ' · 本地样例' : ''}` : '未保存'}
            tone={job ? 'success' : 'neutral'}
          />
          <span className="small muted">
            {job
              ? job.sample
                ? '演示版本，仅保存在浏览器本地，不会写入后端。'
                : '岗位版本由后端保存；修改 JD 后旧分析会标记过期。'
              : '保存后才会创建岗位版本；未保存的 JD 不会写入后端。'}
          </span>
        </div>
        {dirty && (
          <span className="jd-dirty" role="status">
            有未保存修改
          </span>
        )}
      </div>
      <div className="form-actions jd-version-actions">
        {sampleMode ? (
          <button
            type="button"
            className="text-button"
            data-confirmed={(confirmed && !dirty) || undefined}
            onClick={onSampleSave}
            disabled={!canSave || busy || (job?.sample === true && !dirty)}
          >
            {confirmed && !dirty ? '✓ 本地样例已保存' : '保存为本地样例'}
          </button>
        ) : (
          <button
            type="button"
            className="primary-button"
            data-confirmed={(confirmed && !dirty) || undefined}
            aria-busy={busy}
            onClick={onSave}
            disabled={saveDisabled}
          >
            {busy
              ? '保存中…'
              : confirmed && !dirty
                ? '✓ 岗位已保存'
                : job && !job.sample && !dirty
                  ? '岗位已保存'
                  : job && !job.sample
                    ? '保存新版本'
                    : '保存岗位'}
          </button>
        )}
      </div>
      {notice && (
        <p
          className={
            notice.tone === 'success'
              ? 'notice-text small'
              : notice.tone === 'warning'
                ? 'jd-warning small'
                : 'error-text small'
          }
          role="status"
        >
          {notice.text}
          {notice.tone === 'danger' && (
            <button
              type="button"
              className="text-button inline-retry"
              disabled={!canSave || busy}
              onClick={sampleMode ? onSampleSave : onSave}
            >
              重试保存
            </button>
          )}
        </p>
      )}
      {stale && (
        <p className="jd-stale small" role="status">
          岗位已更新，之前的分析可能已过期，请重新分析后再生成计划。
        </p>
      )}
    </div>
  );
}
