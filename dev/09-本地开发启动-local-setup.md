# CareerPilot 本地开发启动

当前提供可启动代码骨架，版本 0.1.0。前端的合成样例展示、API 健康/依赖检查、公共模型、未实现路由、Worker 诊断入口和 PR 检查已经加入代码；登录、真实上传、JD 保存和完整分析 API 仍待接入。C 已实现 Markdown/TXT 入库及候选检索模块（见下文），D 已加入 LangGraph 分析模块；PDF 解析、真实生成模型与 B/C/D 全链路尚待完成，不能把模块就绪当成 M1 验收通过。

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
- GET /api/v1/meta：mode=scaffold，业务 feature 均为 false，不能把依赖 ready 当作 M1 完成。
- 所有已登记的业务接口：有效参数返回 501 NOT_IMPLEMENTED；参数无效返回统一 422；当前没有假令牌、假用户或假模型结果。

没有 Docker 时仍可以启动前后端查看骨架，依赖检查显示未就绪；这不构成基础设施联调完成。

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
