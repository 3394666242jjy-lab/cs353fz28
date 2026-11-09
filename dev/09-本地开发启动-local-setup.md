# CareerPilot 本地开发启动

当前版本 0.1.0。本地账号、上传、PDF/Markdown/TXT 解析、索引、分析任务和证据查询已接入；运行前需配置 MongoDB、Redis、Qdrant 和模型，并在目标环境做真实联调。

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

macOS/Linux 对应命令为 `test -f .env || cp .env.example .env`。配置文件只在本地保留，不提交。API 可以在没有模型密钥时启动，但完成真实索引和分析前必须配置可用的 Embedding 与生成模型服务。

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
- GET /api/v1/meta：列出已实现的身份认证、文档上传、岗位导入、RAG 和分析能力；这不检查模型或依赖是否可用，也不代表端到端验收通过。
- 登录、岗位、资料上传、执行状态、分析和证据接口均已接入；实际使用需要相应依赖和模型配置。参数无效时返回统一 422。

没有 Docker 时仍可以启动前后端查看骨架，依赖检查显示未就绪；这不构成基础设施联调完成。

### 本地种子账号与登录

在项目根目录执行 `uv run --project backend python scripts/seed_user.py`。未配置 SEED_PASSWORD 时，在终端输入两次自选测试密码（至少 12 字符，输入隐藏）。也可通过已忽略的根目录 .env 配置 SEED_EMAIL、SEED_DISPLAY_NAME、SEED_PASSWORD，禁止提交有效密码。默认邮箱 demo@example.invalid；重复执行不会重置已有账号密码。不要在脚本命令行参数中传密码。

在 /docs 展开 POST /api/v1/auth/login，使用邮箱和自选密码测试。200 返回 access_token、token_type、expires_in。复制原始 access_token，在顶部 Authorize 的 HTTPBearer 输入框粘贴后确认，再调用 GET /api/v1/me，应只返回本人 id、email、display_name。密码错误、没有令牌或令牌过期返回 401；MongoDB 停止时登录返回 503。勿在截图、日志或 GitHub 中保存密码、有效令牌。

真实 MongoDB 自查可在 PowerShell 设置 `$env:RUN_MONGO_AUTH_TESTS = "1"` 后运行后端 pytest；测试使用 careerpilot_auth_test 库内独立生成的合成账号，只清理本次生成的用户和会话，不读取或改动本地种子账号。默认不设置此变量时跳过此项。该自查验证账号创建、重复初始化保护、登录、本人资料、缺失令牌和过期会话；完整用户隔离仍需在真实业务链路中验收。

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

页面操作：先使用种子账号登录，再上传资料并等待就绪，填写岗位描述并发起分析，最后点击证据查看原文及页码或行号。也可不登录使用合成样例演练。

生产预览可使用 `npm run build` 后运行 `npm run preview`。preview 仅提供静态文件，不包含开发代理；独立 API 部署时需在构建前设置公开的 VITE_API_BASE_URL，或配置反向代理。不要将服务端密钥命名为 VITE_ 开头。

## 模块与交接

| 负责人 | 主要代码入口 | 仍需完成 |
| --- | --- | --- |
| A 前端 | frontend/src/pages、components、api/client.ts | 登录、上传、轮询及结果页面已接入；继续验证真实服务和交互 |
| B 后端 | backend/app/api、services、config.py、worker.py | 鉴权、文件校验/私有存储、持久化、真实任务分发、调用 C 的 PDF/文本解析并保存结果与失败状态 |
| C RAG | backend/app/rag/（chunking、embeddings、vector_store、indexer、retrieval） | PDF 逐页解析及证据检索已接入；继续评估检索质量 |
| D Agent | backend/app/agent/pipeline.py、contracts.py | 真实生成模型、B/C 完整 Evidence 适配器与 PDF 全链路联调 |
| 项目负责人 | 文档、schemas 评审、Compose 和发布配置 | 确认公共契约，整合模块，最终测试、ECS 部署与报告 |

RAG 支持文本层 PDF 与 UTF-8 Markdown/TXT，PDF 引用按页定位；证据原文由 MongoDB 提供。LangGraph 接入说明见 [10 Agent 模块接入](10-Agent模块接入-agent-guide.md)。真实模型与存储仍需在目标环境联调；后续依赖使用 `uv add` 并提交锁文件。

PDF 提取结果按原页码保存，包括空白页。为了避免静默遗漏图形中的正文，当前采用严格保守模式：包含图片（包括头像或 Logo）、矢量绘制、注释、未知绘制指令或无法确认内容的 PDF 会被拒绝；不提供 OCR。请将内容转换为仅含可复制文字的 PDF，或上传 UTF-8 TXT/Markdown。即使文本能够复制，上传前也请核对解析结果和引用；安全处理不能保证任意 PDF 的视觉排版和提取文本完全一致。

任务状态采用 Worker 心跳和后台状态协调：默认排队超过 3600 秒或运行时超过 300 秒未收到心跳后，API 会定期尝试协调资源与任务状态，查询时也会触发检查。可在 `.env` 中按环境设置 `EXECUTION_QUEUE_TIMEOUT_SECONDS` 和 `EXECUTION_LEASE_SECONDS`；超时不是实时进度保证。索引中断后可能留下不可见的未完成索引数据，失败资料请重新上传；这不是 M2 的完整故障补偿和重试机制。

M1 要求有文本层 PDF 上传和 Python 后端解析，当前代码已接入解析器与 Worker。本地验收仍须上传独立合成的多页 PDF，核对原 PDF 页码、空白页后的定位和失败状态，不能只用 Markdown 或固定样例宣布 M1 完成。详细要求见 [08 的 PDF 交付与验收](08-M1开工计划-kickoff-plan.md)。

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
| `RETRIEVAL_STRATEGY` | `dense` | 三档检索：`dense` / `hybrid` / `hybrid_rerank`（M2） |
| `HYBRID_SPARSE_WEIGHT` | `0.30` | 稀疏通道占比，三档对比必须锁定同一值；由开发集扫描得出，见 [13 第 5 节](13-M2检索策略与评测-rag-m2-spec.md) |
| `HYBRID_CANDIDATES_PER_CHANNEL` | 20 | 融合前每路候选数 |
| `RERANK_URL` / `RERANK_MODEL` | 百炼原生 rerank 端点 / `gte-rerank-v2` | `qwen-rerank` 在部分账号返回 403，需按账号实测 |
| `RERANK_API_KEY` | 空 | 留空时复用 `EMBEDDING_API_KEY`（两者共用同一个百炼 Key）|

集合结构自 M2 起要求 `dense` + `sparse` 两个命名字段；M1 时期建的集合缺少 `sparse`
时 `ensure_collection()` 会给出"删除集合并重建索引"的可执行错误，不会静默降级。只写
稠密向量的旧代码路径在新集合上仍可正常工作。

本地验证（无需模型 Key 的部分）：

```powershell
uv run --project backend pytest backend/tests -q
```

真实服务验证（需 `EMBEDDING_API_KEY` 与可达的 Qdrant）：执行合成样例的解析→切分→入库→检索链路，确认三个需求查询（数据库设计/缓存/Docker）的候选排序和跨用户隔离；已发布版本不可原地重建，更新资料需使用新版本；检索结果为 chunk 元数据，quote 与 locator 由后端查 MongoDB 补齐。当前 Qdrant 指向云端集群的配置为本地 `.env` 设置，不随仓库提交。

M2 的三档检索与评测有独立的可复跑路径（不需要 MongoDB/Celery），命令与口径见
[14 C 开发检索记录](14-C开发检索记录-rag-retrieval-log.md) 第 5 节；评测运行器与调参脚本
分别是 `scripts/m2_rag_eval.py` 与 `scripts/m2_rag_tune.py`，规则（调参只用开发集、
评测集只报告一次）见 [13](13-M2检索策略与评测-rag-m2-spec.md)。

### 独立 M1 端到端验收

启动 API、Worker 和全部依赖，配置真实 Embedding 与生成模型后，使用两个不同的本地测试账号执行：

```powershell
uv run --project backend python scripts/m1_acceptance.py --base-url http://127.0.0.1:8000 --timeout 600
```

脚本会提示输入两个账号和密码（密码不回显），或从 `M1_USER_EMAIL`、`M1_USER_PASSWORD`、`M1_OTHER_EMAIL`、`M1_OTHER_PASSWORD` 读取。脚本会在第一个账号中新增合成资料、岗位和分析记录，M1 没有删除功能，因此请只使用测试账号，不要使用个人或生产资料。它会检查 PDF 空白页后的页码、文本行号、伪装格式和跨用户隔离，并输出模型实际结论供人工核对；它无法独立证明服务端模型不是替身。没有真实模型或 Worker 时，不能将脚本的输入自查视为整链验收通过。只自查合成资料可运行 `uv run --project backend python scripts/m1_acceptance.py --self-test`。

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

依赖启动较慢时可运行 `uv run --project backend python scripts/check_infra.py --attempts 20`。该脚本只检查连接，不创建业务数据、用户或索引。Worker 可通过 `uv run --project backend celery -A app.worker.celery_app worker --pool=solo --loglevel=info` 启动，处理文档索引和分析任务；真实依赖与模型均需要配置。

PR 的 Scaffold checks 工作流验证后端检查与契约、真实 Compose 依赖连接、前端类型和构建、生成契约一致性。它不部署 ECS、不访问模型或生产密钥。状态以当前 PR 的真实检查结果为准。

开发中已验证的范围与后续验收分开记录：本次框架检查通过不等于 #6–#9 的业务任务完成，也不等于模型评测成绩。
