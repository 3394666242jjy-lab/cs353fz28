# CareerPilot M1 开工计划

状态：M1 开工契约 v0.3 候选版，待负责人评审确认。本文把整体产品设计收敛为首轮开发任务，不代表应用、接口或评测已经完成。

## 第一阶段目标

完成一个本地、合成数据的端到端流程：

```text
登录开发种子账号
  → 上传 Markdown/TXT 简历与项目说明
  → 解析、切分、Embedding、Qdrant 入库
  → 导入一份 JD
  → 创建分析任务并轮询状态
  → LangGraph 拆解要求、调用真实检索、核对证据
  → 展示三类匹配结论
  → 点击引用查看原文与行号
```

M1 只承诺这条链路。准备计划、任务看板、文本面试、PDF/OCR、混合检索、Rerank、人工确认恢复、完整 Outbox 故障补偿、真实 SSE 及云端部署在后续阶段实现。项目整体 P0/P1 优先级保持不变，里程碑 M1/M2/M3 表示实现先后，不能把 M1 当成完整 P0 发布。

## 负责人开工前的交付

| 内容 | 当前材料 | 负责人需要确认 |
| --- | --- | --- |
| 范围与验收 | [PRD](01-需求文档-prd.md) 和本文 | M1 必做流程、错误场景和后续范围 |
| 模块边界 | [架构](02-技术架构-architecture.md) | A/B 公共 API、B/C 入库调用、D/C 检索及 B/D 结果保存边界 |
| 数据设计 | [数据库设计](03-数据库设计-database-schema.md) | M1 字段、归属、版本、状态、引用和索引 |
| 接口样例 | [API](04-API接口文档-api-spec.md) 与 [examples](../examples/README.md) | 请求响应契约、三类匹配语义和原文引用 |
| 首轮任务 | 下方已创建的任务 Issue | 成员认领、依赖和实际可投入时间 |

成员姓名与 GitHub 账号尚未确定，任务保留角色归属并等待认领。负责人确认后，在对应文档或评审记录留存确认版本；后续改变公共字段或范围时同步文档、调用方和任务。

## 四位成员的首轮交付

| 角色 | 本轮任务 | 交付物 | 可并行与依赖 |
| --- | --- | --- | --- |
| 负责人 | [#5 确认契约并组织联调验收](https://github.com/3394666242jjy-lab/cs353fz28/issues/5) | 契约确认、身份认领、真实验收记录 | 先确认公共约定，模块接通后验收；最终测试和部署仍由本人负责 |
| A 前端 | [#6 上传、JD 与带证据分析页面](https://github.com/3394666242jjy-lab/cs353fz28/issues/6) | 在 React/TS 骨架上实现真实资料/JD 输入、轮询状态、结果与引用页面 | 已有显式样例交互；接 B API 后切换真实服务模式 |
| B 后端 | [#7 API、MongoDB 与最小任务链路](https://github.com/3394666242jjy-lab/cs353fz28/issues/7) | 在 FastAPI/Pydantic 骨架上实现种子账号、上传/JD/任务/分析/证据接口、最小 Celery 业务任务 | 已有路由与模型；先实现存储和鉴权，联调时接 C 的入库和 D 的分析 |
| C RAG | [#8 文本入库、Qdrant 与原文定位](https://github.com/3394666242jjy-lab/cs353fz28/issues/8) | 文本解析、切分、Embedding、向量入库、过滤检索、来源定位 | 与 B 约定元数据和存储接口；检索模块可先独立运行 |
| D Agent | [#9 JD 拆解、证据核对与结构化分析](https://github.com/3394666242jjy-lab/cs353fz28/issues/9) | LangGraph 基本流程、检索工具接入、结构化输出及超时控制 | 先用检索替身开发契约，再接 C 的真实检索；B 负责最终保存 |

本轮文档与样例准备由 [#4](https://github.com/3394666242jjy-lab/cs353fz28/issues/4) 跟踪。上表任务不会随文档 PR 合并自动关闭，需成员实际完成后另提 PR 验收。

## 最少接口和数据

- 身份：POST /auth/login、GET /me；本地种子账号由初始化脚本创建，密码放开发者本地配置并存哈希，不放有效凭据进仓库。
- 资料：POST /documents、GET /documents、GET /documents/{id}；M1 支持 Markdown/TXT，完成解析后 ready。
- 岗位：POST /jobs、GET /jobs/{id}；保留原始 JD，M1 不要求岗位编辑界面。
- 分析：POST /analyses、GET /executions/{id}、GET /analyses/{id}、GET /evidence/{chunk_id}?version=1。
- M1 集合：users、documents、document_versions、chunks、jobs、analyses、executions；完整字段和索引见 03。
- 运行组件：前端、API、最小 Celery Worker、Redis、MongoDB、Qdrant 和模型适配器。模块开发可使用明确标记的替身，整链验收必须用真实 MongoDB、Qdrant、Embedding 与 Agent 路径。

M1 文件适配器使用被忽略的本地私有目录。API 从认证上下文确定用户，不能把 user_id 当成前端可随意指定的参数。模型密钥由开发者配置；没有可用服务时应报告依赖未满足，不能用 fixture 冒充真实检索和生成。

M1 后台执行通过 Celery/Redis 最小分发，MongoDB 保存 queued/running/succeeded/failed 和结果，前端轮询。完整 Outbox、暂停/恢复、事件回放属于 M2；M1 不承诺故障不丢消息或生产可靠性。任务创建后如果投递失败，必须标记可查询失败而非永久 queued；应用重启后未恢复的运行任务应明确标记失败，不能无限等待。

## 三类结果与引用契约

开发样例见 [examples/README](../examples/README.md)。supported 必须有对应原文证据；partial 明确已体现和缺失的部分；not_evidenced 的 evidence_refs 必须为空，并表达“资料未体现”。不得输出不存在的指标或用户经历。

证据通过 chunk_id、document_id、version 和 locator 定位，quote 必须来自上传原文。样例中数据库设计位于 project-demo.md 第 11 行，缓存说明位于第 15–16 行。实际分块可以包含更多上下文，只要引用准确，不能为匹配 fixture 强制伪造片段或 ID。

API 保留 snake_case 和统一 envelope。D 的内部 requirements/evidence_ids 由 B 根据本次检索证据映射为公开 matches/evidence_refs，API 附带 requirement 文本与 category，不能让前端自行猜测要求。完整示例是渲染契约，不是后端可以硬编码的固定输出。

## 联调顺序与完成证据

1. 负责人确认契约；A/B 确认 API，B/C/D 确认模块调用；认领各自任务。
2. A 跑通样例页面；B 保存真实资料/JD；C 独立验证索引/检索；D 验证拆解与输出。
3. B 连接 C 的入库、D 的分析和证据保存，A 切到真实 API，跑通完整流程。
4. 负责人执行下列 M1 验收，记录实际命令、截图、响应和已知问题。

- [ ] 两份合成原始资料上传完成并显示 ready；生成的 ID 和版本可追溯。
- [ ] 从 JD 创建真实岗位和分析任务，状态最终明确成功或失败；刷新页面能继续查询。
- [ ] 三个要求输出合理状态，数据库和缓存的引用能回到原文；Docker 无证据时不编造。
- [ ] 资料未就绪返回 409；模型或解析失败能查看原因；错误的资源 ID 返回明确错误。
- [ ] 第二用户不能读取第一用户的文档、分析、任务或证据。
- [ ] 样例 JSON 和预期答案没有进入个人资料索引，真实界面不将 fixture 标成实际分析。
- [ ] 组员交付运行方法、配置变量、模块样例及已完成的自查；不要求负责人补做模块基础自查。

公开开发样例只证明联调协议与基本流程，不能证明系统总体准确率。独立保留验收资料由负责人另行整理；所有真实效果数据来自实际运行。
