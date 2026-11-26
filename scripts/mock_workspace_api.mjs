// Local-only, synthetic UI acceptance service. Never connects to real storage or accounts.
// VITE_API_PROXY_TARGET=http://127.0.0.1:8011 npm run dev -- --port 5175
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
const root = new URL('../', import.meta.url);
const analysis = JSON.parse(await readFile(new URL('examples/analysis-response.json', root), 'utf8')).data;
const evidence = Object.values(JSON.parse(await readFile(new URL('examples/evidence-responses.json', root), 'utf8')).responses).map(item => item.data);
const documents = new Map([
  ['mock-ready', { id: 'mock-ready', kind: 'resume', filename: '合成简历.md', current_version: 1, status: 'ready', error: null }],
  ['mock-failed', { id: 'mock-failed', kind: 'project', filename: '待重试项目.md', current_version: 2, status: 'failed', error: '模拟：解析服务暂时不可用。' }],
]);
const executions = new Map();
const attempts = new Map();
const jobs = new Map();
let evidenceAttempt = 0;
function reply(res, data, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ data, request_id: 'synthetic-workspace' })); }
function fail(res, message, status = 503) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { code: 'MOCK_FAILURE', message }, request_id: 'synthetic-workspace' })); }
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1:8011');
    const path = url.pathname;
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    const json = () => JSON.parse(body.toString() || '{}');
    if (path === '/health/live') return reply(res, { status: 'alive' });
    if (path === '/api/v1/meta') return reply(res, { version: 'synthetic', capabilities: [] });
    if (path === '/api/v1/auth/login') return reply(res, { access_token: 'synthetic-local-token', token_type: 'bearer' });
    if (path === '/api/v1/me') return reply(res, { id: 'synthetic-local-user', email: 'demo@example.test', display_name: '本地验收样例' });
    if (path === '/api/v1/documents' && req.method === 'GET') return reply(res, { items: [...documents.values()], next_cursor: null });
    if (path === '/api/v1/documents' && req.method === 'POST') {
      const form = await new Request(url, { method: 'POST', headers: req.headers, body }).formData();
      const file = form.get('file');
      const attempt = (attempts.get(file.name) ?? 0) + 1;
      attempts.set(file.name, attempt);
      await delay(1300);
      if (attempt === 1) return fail(res, '模拟：上传连接中断，请重试。');
      const id = `mock-upload-${documents.size}`;
      documents.set(id, { id, kind: form.get('kind'), filename: file.name, current_version: 1, status: 'uploaded', error: null });
      setTimeout(() => { documents.get(id).status = 'processing'; }, 1800);
      setTimeout(() => { documents.get(id).status = 'ready'; }, 5500);
      return reply(res, { document_id: id, execution_id: `index-${id}` }, 202);
    }
    const documentRoute = path.match(/^\/api\/v1\/documents\/([^/]+)(\/reindex)?$/);
    if (documentRoute) {
      const doc = documents.get(documentRoute[1]);
      if (!doc) return fail(res, '资料不存在', 404);
      if (documentRoute[2]) {
        if (json().expected_version !== doc.current_version) return fail(res, '版本冲突', 409);
        const id = `retry-${doc.id}`;
        executions.set(id, { id, type: 'index', status: 'running', state_version: 1 });
        // Keep the old failed document status until the worker starts, exercising retry polling.
        setTimeout(() => { doc.status = 'processing'; doc.error = null; }, 2300);
        setTimeout(() => { doc.status = 'ready'; executions.get(id).status = 'succeeded'; }, 5500);
        return reply(res, { document_id: doc.id, version: doc.current_version, execution_id: id }, 202);
      }
      return reply(res, { ...doc, extraction: { kind: 'text', text: '合成验收资料，不代表真实个人经历。' } });
    }
    if (path === '/api/v1/jobs' && req.method === 'POST') {
      const data = json();
      const id = `mock-job-${jobs.size}`;
      jobs.set(id, { id, version: 1, ...data });
      return reply(res, { job_id: id, version: 1 }, 201);
    }
    if (path.startsWith('/api/v1/jobs/')) return reply(res, jobs.get(path.split('/').at(-1)));
    if (path === '/api/v1/analyses' && req.method === 'POST') {
      const id = 'mock-analysis-execution';
      executions.set(id, { id, type: 'analysis', status: 'running', state_version: 1 });
      setTimeout(() => { Object.assign(executions.get(id), { status: 'succeeded', result_id: analysis.id, result_type: 'analysis' }); }, 2300);
      return reply(res, { execution_id: id }, 202);
    }
    if (path.endsWith('/events')) return fail(res, '模拟服务使用轮询', 404);
    if (path.startsWith('/api/v1/executions/')) return reply(res, { pending_input: null, error: null, result_id: null, result_type: null, latest_event_id: null, ...executions.get(path.split('/').at(-1)) });
    if (path.startsWith('/api/v1/analyses/')) return reply(res, analysis);
    if (path.startsWith('/api/v1/evidence/')) {
      const current = ++evidenceAttempt;
      await delay(current === 1 ? 1400 : 650);
      if (current === 1) return fail(res, '模拟：原文暂时未能加载。');
      const item = evidence.find(item => item.chunk_id === decodeURIComponent(path.split('/').at(-1)) && item.version === Number(url.searchParams.get('version')));
      return item ? reply(res, item) : fail(res, '原文不存在', 404);
    }
    return fail(res, '本地验收服务未提供此接口', 404);
  } catch (err) { console.error(err.message); fail(res, '本地验收服务错误', 500); }
});
server.listen(8011, '127.0.0.1', () => console.log('Synthetic workspace API: http://127.0.0.1:8011'));
