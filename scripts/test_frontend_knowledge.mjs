// Run from repo root: node scripts/test_frontend_knowledge.mjs
// Exercise the actual API client and latest-request guard; no live credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
async function loadTs(path, transform = value => value) {
  const raw = transform(await readFile(new URL(path, import.meta.url), 'utf8'));
  const js = ts.transpileModule(raw, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
}
const { api, ApiError, setAuthToken } = await loadTs('../frontend/src/api/client.ts',
  raw => raw.replace('import.meta.env.VITE_API_BASE_URL', "'http://test.invalid'"));
const { LatestRequest } = await loadTs('../frontend/src/api/latest-request.ts');
const values = new Map();
globalThis.sessionStorage = {
  getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: key => values.delete(key),
};
const events = [];
globalThis.window = { dispatchEvent: event => events.push(event.type) };
const envelope = data => Response.json({ data, request_id: 'test-request' });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

test('catalog uses business endpoint, role and current bearer', async () => {
  setAuthToken('synthetic-account-a');
  globalThis.fetch = async (url, init) => {
    assert.equal(url, 'http://test.invalid/api/v1/knowledge/sources?role=fullstack');
    assert.equal(init.method, 'GET');
    assert.equal(init.headers.Authorization, 'Bearer synthetic-account-a');
    return envelope({ items: [] });
  };
  assert.deepEqual((await api.listKnowledgeSources('fullstack')).data.items, []);
});

test('query transmits exact filters/budget and preserves source version, text and empty results', async () => {
  const body = { role: 'agent_engineer', query: '恢复流程', topics: ['checkpoint'],
    source_version: '2026-11-13.r1', top_k: 4, max_chars: 6000 };
  const item = { source_id: 'test-source', source_version: '2026-11-13.r1',
    source_url: 'https://example.com/reference', title: '恢复', summary: '逐步恢复\n保留边界',
    roles: ['agent_engineer'], content_kind: 'editorial_summary', usage: 'reference_only' };
  globalThis.fetch = async (url, init) => {
    assert.equal(url, 'http://test.invalid/api/v1/knowledge/query');
    assert.equal(init.method, 'POST');
    assert.equal(init.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(init.body), body);
    return envelope({ items: [item], notice: '学习参考' });
  };
  assert.deepEqual((await api.queryKnowledge(body)).data.items, [item]);
  globalThis.fetch = async () => envelope({ items: [], notice: '没有命中' });
  assert.deepEqual((await api.queryKnowledge(body)).data.items, []);
});

test('cancel remains combined with a 30s timeout', async () => {
  const originalTimeout = AbortSignal.timeout;
  const timeout = new AbortController();
  AbortSignal.timeout = ms => { assert.equal(ms, 30000); return timeout.signal; };
  try {
    const caller = new AbortController();
    let combined;
    globalThis.fetch = async (_url, init) => { combined = init.signal; return envelope({items: []}); };
    await api.listKnowledgeSources('fullstack', caller.signal);
    assert.equal(combined.aborted, false);
    caller.abort();
    assert.equal(combined.aborted, true);
    await api.queryKnowledge({role:'fullstack', query:'test'}, new AbortController().signal);
    timeout.abort(new DOMException('Timed out', 'TimeoutError'));
    assert.equal(combined.reason.name, 'TimeoutError');
  } finally { AbortSignal.timeout = originalTimeout; }
});

test('late request cannot replace newer role/query, even if fetch ignores abort', async () => {
  const guard = new LatestRequest();
  const firstResponse = deferred();
  const secondResponse = deferred();
  let calls = 0;
  globalThis.fetch = () => calls++ === 0 ? firstResponse.promise : secondResponse.promise;
  let displayed;
  const first = guard.start();
  const firstRun = api.listKnowledgeSources('fullstack', first.signal).then(({data}) => {
    if (first.isCurrent()) displayed = data;
  });
  const second = guard.start();
  assert.equal(first.signal.aborted, true);
  const secondRun = api.listKnowledgeSources('agent_engineer', second.signal).then(({data}) => {
    if (second.isCurrent()) displayed = data;
  });
  secondResponse.resolve(envelope({items: [{id:'new'}]}));
  await secondRun;
  firstResponse.resolve(envelope({items: [{id:'old'}]}));
  await firstRun;
  assert.deepEqual(displayed, {items:[{id:'new'}]});
  guard.cancel(); // Used by cancel, editing filters, unmount and account/role remount.
  assert.equal(second.isCurrent(), false);
  assert.equal(second.signal.aborted, true);
});

test('401 expires current session, but an old account response cannot expire a new login', async () => {
  events.length = 0;
  setAuthToken('synthetic-account-a');
  globalThis.fetch = async () => Response.json({error:{code:'AUTH',message:'expired'},request_id:'expired-id'}, {status:401});
  await assert.rejects(api.listKnowledgeSources('fullstack'), error =>
    error instanceof ApiError && error.status === 401 && error.requestId === 'expired-id');
  assert.deepEqual(events, ['careerpilot:session-expired']);
  events.length = 0;
  const response = deferred();
  globalThis.fetch = () => response.promise;
  const pending = api.queryKnowledge({role:'fullstack',query:'test'});
  setAuthToken('synthetic-account-b');
  response.resolve(new Response('', {status:401}));
  await assert.rejects(pending, {status:401});
  assert.deepEqual(events, []);
});

test('undeployed, unavailable and invalid envelopes remain errors instead of empty success', async () => {
  for (const status of [404, 501, 503]) {
    globalThis.fetch = async () => Response.json({error:{code:'UNAVAILABLE',message:'unavailable'},request_id:'failure-id'}, {status});
    await assert.rejects(api.listKnowledgeSources('fullstack'), {status, requestId:'failure-id'});
  }
  globalThis.fetch = async () => Response.json({items:[]});
  await assert.rejects(api.queryKnowledge({role:'fullstack',query:'test'}), ApiError);
});
