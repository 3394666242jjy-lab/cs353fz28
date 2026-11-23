import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
async function load(path, transform = value => value) {
  const raw = transform(await readFile(new URL(path, import.meta.url), 'utf8'));
  const js = ts.transpileModule(raw, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
}
const { analysisBlockers } = await load('../frontend/src/api/analysis-readiness.ts');
const ready = { loggedIn: true, documentCount: 1, title: '工程师', jd: '岗位要求', saved: true, dirty: false };
test('ready real inputs allow analysis; whitespace cannot masquerade as a job', () => {
  assert.deepEqual(analysisBlockers(ready), []);
  assert.deepEqual(analysisBlockers({ ...ready, title: '  ', jd: '\n' }).map(item => item.action), ['job']);
});
test('missing prerequisites lead to distinct recovery destinations', () => {
  assert.deepEqual(analysisBlockers({ ...ready, loggedIn: false, documentCount: 0, saved: false }).map(item => item.action), ['login', 'documents', 'job']);
});
test('unsaved or synthetic jobs and newer unsaved edits cannot start real analysis', () => {
  assert.equal(analysisBlockers({ ...ready, saved: false })[0].action, 'job');
  assert.match(analysisBlockers({ ...ready, dirty: true })[0].message, /最新修改/);
});
const { api, setAuthToken } = await load('../frontend/src/api/client.ts', raw => raw.replace('import.meta.env.VITE_API_BASE_URL', "'http://test.invalid'"));
const values = new Map();
globalThis.sessionStorage = { getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value), removeItem: key => values.delete(key) };
test('retry targets the same document/version without uploading a duplicate file', async () => {
  setAuthToken('synthetic');
  const controller = new AbortController();
  globalThis.fetch = async (url, init) => {
    assert.equal(url, 'http://test.invalid/api/v1/documents/doc%2F1/reindex');
    assert.equal(init.method, 'POST');
    assert.deepEqual(JSON.parse(init.body), { expected_version: 3 });
    assert.equal(init.signal, controller.signal);
    return Response.json({ data: { execution_id: 'retry-1' }, request_id: 'synthetic' });
  };
  assert.equal((await api.reindexDocument('doc/1', { expected_version: 3 }, controller.signal)).data.execution_id, 'retry-1');
});
test('evidence requests pin the cited version and carry cancellation', async () => {
  const controller = new AbortController();
  globalThis.fetch = async (url, init) => {
    assert.equal(url, 'http://test.invalid/api/v1/evidence/chunk%2F1?version=2');
    assert.equal(init.signal, controller.signal);
    return Response.json({ data: { version: 2, quote: '原文' }, request_id: 'synthetic' });
  };
  assert.equal((await api.getEvidence('chunk/1', 2, controller.signal)).data.quote, '原文');
});



const { glassWords, readingInfluence } = await load('../frontend/src/components/glass-reading.ts');
test('each Chinese character responds independently while grapheme clusters stay intact', () => {
  assert.deepEqual(glassWords('数据建模'), ['数','据','建','模']);
  assert.deepEqual(glassWords('A👩‍💻é'), ['A','👩‍💻','é']);
});
test('visual word segmentation preserves full original including punctuation and line breaks', () => {
  for (const text of ['设计字段、关联关系与查询索引。', 'Redis 缓存与 MongoDB。\n未提供测量记录。', '经历 👩‍💻 与 API v1']) {
    assert.equal(glassWords(text).join(''), text);
    assert.ok(glassWords(text).length > 1);
  }
});
test('Dock-like reading has smooth local falloff without affecting distant lines', () => {
  assert.equal(readingInfluence(0,0),1);
  assert.equal(readingInfluence(94,0),0);
  assert.equal(readingInfluence(0,38),0);
  assert.equal(readingInfluence(500,500),0);
  assert.ok(readingInfluence(20,0)>readingInfluence(50,0));
  assert.equal(readingInfluence(20,10),readingInfluence(-20,-10));
  assert.ok(Math.abs(readingInfluence(40,0)-readingInfluence(40.1,0))<.005);
});

const { parseReadingState, highlightParts } = await load('../frontend/src/components/reading-state.ts');
test('reading preferences validate versions, roles, identifiers and bounded scroll positions', () => {
  assert.equal(parseReadingState('{broken').role, 'ai_product_manager');
  assert.equal(parseReadingState(JSON.stringify({ version: 2, role: 'fullstack' })).role, 'ai_product_manager');
  const state = parseReadingState(JSON.stringify({ version: 1, role: 'fullstack', query: 'React 状态', topic: 'React', selectedId: 'react-state', pageY: 420, listY: -10 }));
  assert.equal(state.role, 'fullstack');
  assert.equal(state.query, 'React 状态');
  assert.equal(state.selectedId, 'react-state');
  assert.equal(state.pageY, 420);
  assert.equal(state.listY, 0);
  assert.equal(parseReadingState(JSON.stringify({ version: 1, pageY: 1000000 })).pageY, 100000);
});
test('search highlights literal tokens and merges overlaps without changing source text', () => {
  const source = 'C++ [API] React 组件和状态';
  const parts = highlightParts(source, 'C++ [API] react');
  assert.equal(parts.map(part => part.text).join(''), source);
  assert.deepEqual(parts.filter(part => part.match).map(part => part.text), ['C++', '[API]', 'React']);
  assert.deepEqual(highlightParts('React', 'Re React'), [{ text: 'React', match: true }]);
  assert.equal(highlightParts('原文', '').map(part => part.text).join(''), '原文');
});

const { reconcileDocumentSnapshot } = await load('../frontend/src/api/upload-state.ts');
test('returning during a retry preserves execution tracking until it reaches a terminal state', () => {
  const pending = { documentId: 'doc1', status: 'processing', retryExecutionId: 'retry1' };
  const snapshot = { documentId: 'doc1', status: 'failed', error: 'old failure' };
  const result = reconcileDocumentSnapshot([pending], [snapshot]);
  assert.equal(result[0].status, 'processing');
  assert.equal(result[0].retryExecutionId, 'retry1');
  assert.equal(result[0].error, undefined);
  assert.equal(reconcileDocumentSnapshot([pending], [{ documentId: 'doc1', status: 'ready' }])[0].retryExecutionId, undefined);
});
