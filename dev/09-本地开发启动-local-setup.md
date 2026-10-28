# CareerPilot 本地开发启动

当前版本 0.1.0。本地种子账号、登录、岗位创建查询及文档和任务状态查询已接入；前端上传与分析页面、PDF/Markdown/TXT 的 RAG 解析和 LangGraph 模块已提供。上传任务、分析 API 及真实模型全链路仍需后续分支接入与联调。

## 环境准备

- Node.js 22.12 或以上，推荐 Node 22/24 LTS；使用 npm 和提交的 package-lock.json。
- Python 3.12 与 uv；后端用 uv.lock 锁定依赖，禁止修改系统 Python 作为项目环境。
- Docker Desktop（Windows/macOS）或 Docker Engine + Compose 插件（Linux），只用于本地 MongoDB、Redis、Qdrant。
- Windows 可使用 PowerShell；完整 Celery 业务与容器部署优先在 Linux/WSL2 验证。

安装工具参考：[Node.js](https://nodejs.org/)、[uv](https://docs.astral.sh/uv/getting-started/installation/)、[Docker](https://docs.docker.com/get-started/get-docker/)。

## 1 拉取并准备配置

PR 合并后，从仓库 main 拉取；合并前评审可切换 PR 的工作分支。以下命令均从项目根目录开始，首次克隆时执行：

```powershell
git clone https://github.com/3394666242jjy-lab/cs353fz28.git
cd cs353fz28
```

仅在本地还没有 .env 时复制配置，避免覆盖已有密钥：

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

macOS/Linux 对应命令为 `test -f .env || cp .env.example .env`。配置文件只在本地保留，不提交。框架无需模型 API Key 即可启动；之后 C/D 接入实际模型时再补齐服务端变量。

## 2 启动开发依赖

确认 Docker 已运行，在根目录执行：

```powershell
docker compose up -d
docker compose ps
```

默认本机端口：MongoDB 27017、Redis 6379、Qdrant 6333。Compose 仅绑定 127.0.0.1，持久化在命名数据卷；这份文件不用于生产环境。本地 MongoDB 是单节点开发实例，M2 计划事务须改用可支持事务的副本集配置或托管实例。

如端口被占用，修改 .env 的 MONGO_HOST_PORT、REDIS_HOST_PORT、QDRANT_HOST_PORT，同时修改 MONGODB_URI、CELERY_BROKER_URL、VECTOR_STORE_URL 对应地址。检查连接无需调用模型。

停止并保留开发数据：`docker compose stop`。移除容器但保留命名数据卷：`docker compose down`。不要随意添加 --volumes，以免清除自己或组员的本地数据。

## 3 启动后端（终端一）

```powershell
cd backend
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- API 文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。
- GET /health/live：200 表示进程可用，不检查数据库。
- GET /health/ready：检查实际 MongoDB、Redis、Qdrant；全部可用才返回 200，缺少服务返回 503 和脱敏状态。
- GET /api/v1/meta：mode=scaffold，authentication=true、job_import=true，其余业务 feature=false，不能把依赖 ready 当作 M1 完成。
- POST /api/v1/auth/login、GET /api/v1/me、POST /api/v1/jobs、GET /api/v1/jobs/{id} 已接真实 MongoDB；其余业务接口有效参数返回 501 NOT_IMPLEMENTED，参数无效返回统一 422。

没有 Docker 时仍可以启动前后端查看骨架，依赖检查显示未就绪；这不构成基础设施联调完成。

### 本地种子账号与登录

在项目根目录执行 `uv run --project backend python scripts/seed_user.py`。未配置 SEED_PASSWORD 时，在终端输入两次自选测试密码（至少 12 字符，输入隐藏）。也可通过已忽略的根目录 .env 配置 SEED_EMAIL、SEED_DISPLAY_NAME、SEED_PASSWORD，禁止提交有效密码。默认邮箱 demo@example.invalid；重复执行不会重置已有账号密码。不要在脚本命令行参数中传密码。

在 /docs 展开 POST /api/v1/auth/login，使用邮箱和自选密码测试。200 返回 access_token、token_type、expires_in。复制原始 access_token，在顶部 Authorize 的 HTTPBearer 输入框粘贴后确认，再调用 GET /api/v1/me，应只返回本人 id、email、display_name。密码错误、没有令牌或令牌过期返回 401；MongoDB 停止时登录返回 503。勿在截图、日志或 GitHub 中保存密码、有效令牌。

真实 MongoDB 自查可在 PowerShell 设置 `$env:RUN_MONGO_AUTH_TESTS = "1"` 后运行后端 pytest；测试使用 careerpilot_auth_test 库内独立生成的合成账号，只清理本次生成的用户和会话，不读取或改动本地种子账号。默认不设置此变量时跳过此项。该自查验证账号创建、重复初始化保护、登录、本人资料、缺失令牌和过期会话，其他业务的跨用户隔离仍待实现后验证。

### JD 创建与查询自查

登录并在 /docs 的 Authorize 中填入令牌后，POST /api/v1/jobs 输入 title、company（可省略）、raw_jd，成功返回 201。复制真实返回的 job_id 到 GET /api/v1/jobs/{job_id}，成功返回 200 和保存的岗位原文，version=1。换另一账号的令牌查询该 ID，应与不存在的 ID 一样返回 404；不带令牌返回 401。JD 的前后空白和换行保留，纯空白内容返回 422。不要将工作分支开发完成误认为已合并或已部署。

真实 MongoDB 岗位测试：PowerShell 设置 `$env:RUN_MONGO_JOB_TESTS = "1"`，运行 `uv run --project backend pytest backend/tests/test_jobs_integration.py -q`。该测试使用 careerpilot_jobs_test 库中的两位临时合成用户，验证保存、读取、原文保留、跨用户 404 和输入校验，仅清理本次生成的记录。CI 在启动依赖后同时运行登录与岗位的真实 MongoDB 测试；负责人仍需执行整体联调验收。

## 4 启动前端（终端二）

从项目根目录打开另一终端：

```powershell
cd frontend
npm ci
npm run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 将 /api、/health、/docs 和 /openapi.json 代理到 127.0.0.1:8000；同域请求仍由统一 API 客户端处理。若后端使用不同端口，设置根目录 .env 的 VITE_API_PROXY_TARGET 后重启前端。

页面操作：资料准备 → 载入合成样例 → 查看样例分析 → 点击原文证据。选择自己的文件只显示文件名，不上传或保存；修改 JD 后固定样例结果失效，不能伪装成真实分析。实际上传、登录及分析由 A/B/C/D 后续接入。

生产预览可使用 `npm run build` 后运行 `npm run preview`。preview 仅提供静态文件，不包含开发代理；独立 API 部署时需在构建前设置公开的 VITE_API_BASE_URL，或配置反向代理。不要将服务端密钥命名为 VITE_ 开头。

## 模块与交接

| 负责人 | 主要代码入口 | 仍需完成 |
| --- | --- | --- |
| A 前端 | frontend/src/pages、components、api/client.ts | PDF/Markdown/TXT 真实上传、登录、轮询、错误与结果页面；引用区分页码/行号；替换样例模式 |
| B 后端 | backend/app/api、services、config.py、worker.py | 鉴权、文件校验/私有存储、持久化、真实任务分发、调用 C 的 PDF/文本解析并保存结果与失败状态 |
| C RAG | backend/app/rag/（chunking、embeddings、vector_store、indexer、retrieval） | 补齐 Python PDF 逐页解析和页内切分；与 B 约定原文查询接口，接通完整证据检索 |
| D Agent | backend/app/agent/pipeline.py、contracts.py | 真实生成模型、B/C 完整 Evidence 适配器与 PDF 全链路联调 |
| 项目负责人 | 文档、schemas 评审、Compose 和发布配置 | 确认公共契约，整合模块，最终测试、ECS 部署与报告 |

RAG 已实现 UTF-8 Markdown/TXT 解析、切分、Embedding、Qdrant 入库与证据候选检索，候选对应的原文及定位仍需由 B 查询权威存储后补齐；本模块使用已有 httpx 依赖。D 已加入 LangGraph 模块、隔离模型配置与明确标注的离线演示，接入说明见 [10 Agent 模块接入](10-Agent模块接入-agent-guide.md)。真实生成模型与 B/C 全链路尚待联调，API 能力开关不因此提前启用。后续依赖使用 `uv add` 加入后端并提交锁文件。

2026-10-22 的 M1 范围调整已要求有文本层 PDF 上传和 Python 后端解析，但当前 RAG 尚未实现 PDF 解析器，真实上传链路也仍待接入。C 交付时需锁定 PDF 解析依赖，B 接入 Worker；本地验收必须上传合成多页 PDF，核对原 PDF 页码、空白页后的定位和失败状态，不能只用 Markdown 或固定样例宣布 M1 完成。详细要求见 [08 的 PDF 交付与验收](08-M1开工计划-kickoff-plan.md)。

## RAG 模块配置与本地验证（成员 C，#8）

配置变量（根目录 `.env`，样例见 `.env.example`；均可选默认值，在线服务需填 Key）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EMBEDDING_BASE_URL` | 阿里云百炼 OpenAI 兼容端点 | 换模型/服务商改此项，本地 Ollama/vLLM 等同样兼容 |
| `EMBEDDING_API_KEY` | 空 | 在线服务必填；本地免鉴权服务留空 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | `text-embedding-v4` / `1024` | 随索引进 `document_versions` 留档；改维度须重建 Qdrant 索引 |
| `EMBEDDING_BATCH_SIZE` | 10 | provider 单请求输入上限 |
| `VECTOR_STORE_URL` / `VECTOR_STORE_API_KEY` | `http://127.0.0.1:6333` / 空 | 在线集群填 HTTPS 地址与 Key；本地 compose 保持默认且 Key 留空 |
| `VECTOR_STORE_COLLECTION` | `careerpilot_chunks` | 可重建索引集合 |
| `CHUNK_MAX_CHARS` | 500 | 切分目标字符数，进入 `chunking_config_version` |
| `RETRIEVAL_TOP_K` | 5 | 证据候选默认返回数 |

本地验证（无需模型 Key 的部分）：

```powershell
uv run --project backend pytest backend/tests -q
```

真实服务验证（需 `EMBEDDING_API_KEY` 与可达的 Qdrant）：执行合成样例的解析→切分→入库→检索链路，确认三个需求查询（数据库设计/缓存/Docker）的候选排序、跨用户隔离与重复入库幂等；检索结果为 chunk 元数据，quote 与 locator 由后端查 MongoDB 补齐。当前 Qdrant 指向云端集群的配置为本地 `.env` 设置，不随仓库提交。

## 更新统一接口类型

公共请求响应以 backend/app/schemas 的 Pydantic 模型为源，前端 schema.d.ts 和 openapi.json 是生成文件，不手改。修改模型后在根目录执行：

```powershell
uv run --project backend python scripts/export_openapi.py
npm run api:types --prefix frontend
```

一起提交模型、两份生成文件、受影响 fixture、文档和调用方。CI 会重新生成并检查差异。后端 fixture 测试会核对样例 JSON 与模型、引用 quote 与原文行号。

## 检查与验证

根目录运行：

```powershell
uv run --project backend ruff check --config backend/pyproject.toml backend/app backend/tests scripts
uv run --project backend ruff format --config backend/pyproject.toml --check backend/app backend/tests scripts
uv run --project backend pytest backend/tests -q
npm run format:check --prefix frontend
npm run build --prefix frontend
docker compose config --quiet
uv run --project backend python scripts/check_infra.py
```

依赖启动较慢时可运行 `uv run --project backend python scripts/check_infra.py --attempts 20`。该脚本只检查连接，不创建业务数据、用户或索引。Worker 的诊断入口可通过 `uv run --project backend celery -A app.worker.celery_app worker --pool=solo --loglevel=info` 启动；目前只有 infrastructure_ping，不接受文档/分析任务，不写业务结果。

PR 的 Scaffold checks 工作流验证后端检查与契约、真实 Compose 依赖连接、前端类型和构建、生成契约一致性。它不部署 ECS、不访问模型或生产密钥。状态以当前 PR 的真实检查结果为准。

开发中已验证的范围与后续验收分开记录：本次框架检查通过不等于 #6–#9 的业务任务完成，也不等于模型评测成绩。
