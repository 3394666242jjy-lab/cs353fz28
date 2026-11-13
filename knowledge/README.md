# 三方向知识库种子集

覆盖 **AI 产品经理、全栈开发、Agent 开发**。本目录是 M3.1 的可核验知识底座：13 个已读取的一手来源、13 份中文整理摘要和冻结的来源清单；尚未接入线上公共向量库。

所有卡片均为 `editorial_summary`，用途为 `reference_only`：帮助解释概念、设计学习路径和评价方案，不能证明用户具有某项技能或经历。原文链接在 `sources.json`，卡片是项目整理内容，不是官方原文摘录或完整镜像。未进行批量网页抓取，也不把原站全文复制入仓库。

## 方向与来源

| 方向 | 素材 | 可覆盖的知识 |
| --- | --- | --- |
| AI 产品经理 | [Google PAIR 产品练习](https://codelabs.developers.google.com/codelabs/pair-guidebook) | 用户需求、AI 价值、数据与信任 |
| AI 产品经理 / Agent | [Google Rules of ML](https://developers.google.com/machine-learning/guides/rules-of-ml) | 简单基线、产品指标与工程质量 |
| AI 产品经理 / Agent | [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework) | 生命周期中的风险识别与评估 |
| AI 产品经理 / Agent | [Anthropic Agent evals](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) | 任务、轨迹、评价方式与回归 |
| 全栈 | [React Thinking in React](https://react.dev/learn/thinking-in-react) | 组件划分、最小状态、数据流 |
| 全栈 | [MDN Fetch](https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch) | HTTP 状态、异步响应与取消 |
| 全栈 / Agent | [PostgreSQL 18 Transactions](https://www.postgresql.org/docs/18/tutorial-transactions.html) | 原子性、并发可见性与事务边界 |
| 全栈 / Agent | [FastAPI Security](https://fastapi.tiangolo.com/tutorial/security/) | API 身份、安全方案与业务授权职责 |
| 全栈 | [Dockerfile 指南](https://docs.docker.com/get-started/docker-concepts/building-images/writing-a-dockerfile/) | 镜像构建与容器启动 |
| Agent / AI 产品经理 | [Anthropic Building effective agents](https://www.anthropic.com/research/building-effective-agents) | 工作流与动态 Agent 的取舍 |
| Agent | [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | 暂停、恢复和副作用幂等 |
| Agent | [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | 线程执行状态与跨线程知识职责 |
| Agent / 全栈 | [MCP 架构](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture) | host/client/server 与接入边界 |

核验日期：2026-11-13。岗位标签可重叠，5 条匹配 AI 产品经理、6 条匹配全栈、9 条匹配 Agent，共 13 个不同来源。Google PAIR 原指南页面部分内容依赖网页呈现，因此本轮采用可读取的官方 Codelab；没有把无法读取的页面标为已采集素材。

`current`、在线指南和概念文章不等同于项目安装版本。PostgreSQL 使用固定 18 文档路径；MCP 使用固定日期路径；Anthropic 2024 架构文章用于概念参考，不能作为当前 SDK 操作手册。其余在线来源在更新摘要前需重新读取核验。

## 来源记录

每条来源保存 `id`、`url`、`roles`、`topics`、`version`、`version_scope`、`verified_on`、`content_kind`、`usage`、`local_path` 和 `sha256`。这里的 `2026-11-13.r1` 是**本项目整理摘要的版本**，不冒充原网站提供的不可变快照编号。

读取器先检查本地路径、重复 ID 与内容指纹。卡片更新后必须复核摘要和版本，再更新 hash；单纯改 hash 不能算完成复核。后续如需保存原站 HTML/PDF，应另建原始资源记录、正文定位、许可/使用范围及更新策略，不覆盖本批整理摘要。

## 使用

在仓库根目录运行：

```powershell
uv run --project backend python scripts/m3_knowledge_demo.py --catalog --role ai_product_manager
uv run --project backend python scripts/m3_knowledge_demo.py --role fullstack --query "Fetch HTTP 错误 取消"
uv run --project backend python scripts/m3_knowledge_demo.py --role agent_engineer --query "checkpoint 恢复 幂等"
```

演示按岗位过滤后使用本地词法排序，返回带来源 URL 和版本的参考上下文；无命中返回空，不生成回答、不调用模型、不写向量库。它用于验证素材与隔离契约，不是线上 RAG 效果验收。

后续扩充优先级：AI 产品经理补充真实任务评价、失败案例和人机交互；全栈补充 TypeScript、测试、可访问性和可观测性；Agent 补充工具权限、检索评测、记忆和故障恢复。课程/论文/项目案例可作为补充，仍须单独记录来源质量与适用版本。求职 JD 和个人项目资料继续按用户隔离保存，不能混进共享知识集。
