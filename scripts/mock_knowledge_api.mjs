// Frontend-only acceptance fixture, NOT a knowledge service or real login.
// node scripts/mock_knowledge_api.mjs [normal|empty-catalog|undeployed]
// VITE_API_PROXY_TARGET=http://127.0.0.1:9009 npm run dev -- --port 5174
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';

const scenario = process.argv[2] ?? 'normal';
if (!['normal', 'empty-catalog', 'undeployed'].includes(scenario)) throw new Error('Unknown fixture scenario');
const root = new URL('../knowledge/', import.meta.url);
const { sources } = JSON.parse(await readFile(new URL('sources.json', root), 'utf8'));
const notice = '前端验收模拟数据；公共资料仅供学习参考，不能证明个人经历。';
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1:9009');
  const send = (data, status = 200) => {
    res.writeHead(status, {'Content-Type':'application/json; charset=utf-8'});
    res.end(JSON.stringify({data, request_id:'frontend-fixture-only'}));
  };
  const fail = status => {
    res.writeHead(status, {'Content-Type':'application/json; charset=utf-8'});
    res.end(JSON.stringify({error:{code:'FIXTURE_ERROR',message:`前端模拟 ${status} 故障`},request_id:'frontend-fixture-only'}));
  };
  let raw = '';
  for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  if (url.pathname === '/api/v1/auth/login') return send({access_token:'frontend-fixture-only',token_type:'bearer'});
  if (req.headers.authorization !== 'Bearer frontend-fixture-only') return fail(401);
  if (url.pathname === '/api/v1/me') return send({id:'fixture-user',email:'frontend-test@example.com',display_name:'前端模拟验收'});
  if (url.pathname === '/api/v1/documents') return send({items:[],next_cursor:null});
  if (url.pathname === '/api/v1/knowledge/sources') {
    if (scenario === 'undeployed') return fail(404);
    return send({items:scenario === 'empty-catalog' ? [] : sources.filter(source => source.roles.includes(url.searchParams.get('role')))});
  }
  if (url.pathname === '/api/v1/knowledge/query') {
    if (body.query.includes('过期')) return fail(401);
    if (body.query.includes('故障')) return fail(503);
    if (body.query.includes('慢查询')) await new Promise(resolve => setTimeout(resolve, 5000));
    const matches = sources.filter(source => source.roles.includes(body.role) &&
      (!body.source_version || body.source_version === source.version) &&
      (!body.topics?.length || body.topics.some(topic => source.topics.includes(topic))));
    const items = body.query.includes('无匹配') ? [] : await Promise.all(matches.slice(0,2).map(async source => ({
      source_id:source.id,source_version:source.version,source_url:source.url,title:source.title,
      summary:(await readFile(new URL(source.local_path,root),'utf8')).slice(0,900),
      roles:source.roles,content_kind:'editorial_summary',usage:'reference_only',
    })));
    return send({items,notice});
  }
  fail(404);
});
server.listen(9009, '127.0.0.1', () => console.log(`Frontend fixture only at 127.0.0.1:9009 (${scenario}); no real authentication or retrieval. Use dummy credentials.`));
