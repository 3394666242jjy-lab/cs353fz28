export function analysisBlockers(input: {
  loggedIn: boolean;
  documentCount: number;
  title: string;
  jd: string;
  saved: boolean;
  dirty: boolean;
}) {
  const blockers: { action: 'login' | 'documents' | 'job'; message: string }[] = [];
  if (!input.loggedIn) blockers.push({ action: 'login', message: '登录后开始真实分析' });
  if (!input.documentCount)
    blockers.push({ action: 'documents', message: '选择至少一份可使用的资料' });
  if (!input.title.trim() || !input.jd.trim())
    blockers.push({ action: 'job', message: '填写岗位名称和岗位描述' });
  else if (!input.saved || input.dirty)
    blockers.push({
      action: 'job',
      message: input.dirty ? '保存岗位的最新修改' : '保存这次的目标岗位',
    });
  return blockers;
}
