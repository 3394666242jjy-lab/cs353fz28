// Run from the repo root: node scripts/test_frontend_events.mjs
// Test the real TS SSE client with streamed HTTP responses, without a browser.
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
const raw = await readFile(new URL('../frontend/src/api/events.ts', import.meta.url), 'utf8');
const source = raw.replace("import { apiBase, getAuthToken } from './client';", "const apiBase = ''; const getAuthToken = () => 'synthetic-token';");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { subscribeExecutionEvents } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
const expired = [];
globalThis.window = { setTimeout: callback => setTimeout(callback, 0), dispatchEvent: event => expired.push(event.type) };

function stream(chunks) {
  return new Response(new ReadableStream({ start(controller) {
    for (const text of chunks) controller.enqueue(new TextEncoder().encode(text));
    controller.close();
  } }), { headers: { 'Content-Type': 'text/event-stream' } });
}

async function run(fetcher, options = {}) {
  globalThis.fetch = fetcher;
  const events = [];
  const errors = [];
  return await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('SSE client failed to stop')), 2000);
    subscribeExecutionEvents('synthetic-execution', { maxRetries: 2, ...options,
      onEvent: event => events.push(event), onError: error => errors.push(error.code),
      onEnd: reason => { clearTimeout(timeout); resolve({events,errors,reason}); },
    });
  });
}

test('CRLF boundaries split across chunks, duplicate IDs and terminal close', async () => {
  const result = await run(async (url, init) => {
    assert.equal(init.headers.Authorization, 'Bearer synthetic-token');
    return stream(['id: ev1\r\nevent: stage\r\ndata: {"status":"running"}\r', '\n\r\n',
      'id: ev1\r\nevent: stage\r\ndata: {"status":"running"}\r\n\r\n',
      'id: ev2\r\nevent: completed\r\ndata: {"status":"succeeded"}\r\n\r\n']);
  });
  assert.deepEqual(result.events.map(event => event.id), ['ev1','ev2']);
  assert.equal(result.reason, 'terminal');
});

test('disconnect resumes from the last applied cursor', async () => {
  let calls = 0;
  const result = await run(async url => {
    if (calls++ === 0) return stream(['id: ev1\nevent: stage\ndata: {"status":"running"}\n\n']);
    assert.match(url, /after_event_id=ev1/);
    return stream(['id: ev2\nevent: completed\ndata: {"status":"succeeded"}\n\n']);
  });
  assert.equal(result.events.length,2);
});

test('empty streams exhaust retries; expired history and 401 propagate', async () => {
  assert.deepEqual((await run(async () => stream([]))).errors,['NETWORK']);
  assert.deepEqual((await run(async () => new Response('', {status:410}))).errors,['EVENT_HISTORY_EXPIRED']);
  assert.deepEqual((await run(async () => new Response('', {status:401}))).errors,['UNAUTHENTICATED']);
  assert.ok(expired.includes('careerpilot:session-expired'));
});
