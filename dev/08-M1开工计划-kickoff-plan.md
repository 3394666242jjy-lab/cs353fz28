# CareerPilot M1 开工计划

状态：M1 开工契约 v0.4 候选版。2026-10-22 按负责人要求，将有文本层 PDF 上传与 Python 后端解析提前到 M1；其余契约待评审确认。本文定义开发与验收要求，不代表应用、接口或评测已经完成。

## 第一阶段目标

完成一个本地、合成数据的端到端流程：

```text
登录开发种子账号
  → 上传 PDF / Markdown / TXT 简历与项目说明
  → Python 后端解析（PDF 逐页提取文本）、切分、Embedding、Qdrant 入库
  → 导入一份 JD
  → 创建分析任务并轮询状态
  → LangGraph 拆解要求、调用真实检索、核对证据
  → 展示三类匹配结论
  → 点击引用查看原文及 PDF 页码 / 文本行号
```

M1 必须支持有文本层 PDF，以及 UTF-8 Markdown/TXT；用户不需要先把 PDF 转成文本。准备计划、任务看板、文本面试、扫描件 OCR、混合检索、Rerank、人工确认恢复、完整 Outbox 故障补偿、真实 SSE 及云端部署在后续阶段实现。项目整体 P0/P1 优先级保持不变，里程碑 M1/M2/M3 表示实现先后，不能把 M1 当成完整 P0 发布。

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
| A 前端 | [#6 上传、JD 与带证据分析页面](https://github.com/3394666242jjy-lab/cs353fz28/issues/6) | PDF/Markdown/TXT 上传、JD 输入、轮询状态、结果与引用页面；按 PDF 页码或文本行号展示来源 | 已有显式样例交互；接 B API 后切换真实服务模式 |
| B 后端 | [#7 API、MongoDB 与最小任务链路](https://github.com/3394666242jjy-lab/cs353fz28/issues/7) | 种子账号、PDF/文本上传校验及私有存储、JD/任务/分析/证据接口、最小 Celery 业务任务 | Worker 调用 C 的 Python PDF/文本解析与索引模块；B 保存提取结果、页码及失败状态 |
| C RAG | [#8 PDF/文本解析、Qdrant 与原文定位](https://github.com/3394666242jjy-lab/cs353fz28/issues/8) | Python PDF 逐页解析、UTF-8 文本解析、带位置切分、Embedding、向量入库与过滤检索 | 代码位于 backend/app/rag；与 B 约定文件输入、逐页输出和错误类型，与 D 对齐证据契约 |
| D Agent | [#9 JD 拆解、证据核对与结构化分析](https://github.com/3394666242jjy-lab/cs353fz28/issues/9) | LangGraph 基本流程、检索工具接入、结构化输出及超时控制 | 先用检索替身开发契约，再接 C 的真实检索；B 负责最终保存 |

本轮文档与样例准备由 [#4](https://github.com/3394666242jjy-lab/cs353fz28/issues/4) 跟踪。上表任务不会随文档 PR 合并自动关闭，需成员实际完成后另提 PR 验收。

## 最少接口和数据

- 身份：POST /auth/login、GET /me；本地种子账号由初始化脚本创建，密码放开发者本地配置并存哈希，不放有效凭据进仓库。
- 资料：POST /documents、GET /documents、GET /documents/{id}；M1 支持有文本层 PDF 和 UTF-8 Markdown/TXT，单文件最大 10 MiB；解析、切分、索引和元数据持久化均成功后才标记 ready。
- 岗位：POST /jobs、GET /jobs/{id}；保留原始 JD，M1 不要求岗位编辑界面。
- 分析：POST /analyses、GET /executions/{id}、GET /analyses/{id}、GET /evidence/{chunk_id}?version=1。
- M1 集合：users、documents、document_versions、chunks、jobs、analyses、executions；完整字段和索引见 03。
- 运行组件：前端、API、最小 Celery Worker、Redis、MongoDB、Qdrant 和模型适配器。模块开发可使用明确标记的替身，整链验收必须用真实 MongoDB、Qdrant、Embedding 与 Agent 路径。

M1 文件适配器使用被忽略的本地私有目录。API 从认证上下文确定用户，不能把 user_id 当成前端可随意指定的参数。模型密钥由开发者配置；没有可用服务时应报告依赖未满足，不能用 fixture 冒充真实检索和生成。

M1 后台执行通过 Celery/Redis 最小分发，MongoDB 保存 queued/running/succeeded/failed 和结果，前端轮询。完整 Outbox、暂停/恢复、事件回放属于 M2；M1 不承诺故障不丢消息或生产可靠性。任务创建后如果投递失败，必须标记可查询失败而非永久 queued；应用重启后未恢复的运行任务应明确标记失败，不能无限等待。

## PDF 上传与 Python 解析交付

PDF 解析是后端 Python 的 M1 必做模块，不在浏览器中抽取，也不要求用户手动转 Markdown。B 负责业务接入，C 负责解析实现；不能仅在文件选择框加入 `.pdf` 就视为完成。

1. A 允许选择 `.pdf/.md/.txt`，展示大小限制、处理中、就绪和失败原因；点击 PDF 引用时显示真实页码及该页提取的原文片段，Markdown/TXT 保留行号。
2. B 校验实际大小与文件格式，不仅信任扩展名或客户端 MIME；保存原始 PDF 到本地私有目录，创建文档版本和索引任务，再由 Worker 调用 C 的模块。解析和 Embedding 等耗时工作不阻塞 FastAPI 请求。
3. C 在 `backend/app/rag` 实现 Python PDF 解析适配器，按原始顺序返回每页的 1 起始页码和实际提取文本，记录解析器版本；所用解析依赖加入 `backend/pyproject.toml` 和 `uv.lock`。Markdown/TXT 继续使用 UTF-8 与 1 起始行号。
4. PDF 按页切分，单个 chunk 不跨页，使用现有 `PdfLocator`：`{"kind":"pdf","page":2}`；文本使用 `TextLocator`：`{"kind":"text","line_start":15,"line_end":16}`。空白页不产出 chunk，但不能删除后重排后续页码；提取文本与 chunk 保留可核对的空白，不用模型补写或改写简历。
5. C 返回逐页提取结果、chunk 文本/locator、版本及索引元数据；B 将提取结果保存在版本化私有文件中，并保存 document_versions/chunks 记录。MongoDB 的 chunk 文本和 locator 是证据查询的权威来源，不能仅依赖 Qdrant 候选元数据。
6. 损坏、需要密码、无可提取文本或仅扫描图像的 PDF 必须明确失败并提示处理方式，不生成空资料或伪造引用；混合 PDF 如存在无法提取的扫描内容，也应明确提示并中止本轮入库，不能把不完整简历静默标记 ready。OCR 不计入 M1。失败不得标记 ready，文档和执行查询都能获得脱敏错误原因。

上传阶段即可判定的错误按 04 文档返回 413/415/422；已经返回 202 后，解析错误通过文档与执行的 failed/error 返回，不能事后将原上传请求改称 422。后续准备计划、SSE 和云端部署的阶段安排不变。

## 三类结果与引用契约

开发样例见 [examples/README](../examples/README.md)。supported 必须有对应原文证据；partial 明确已体现和缺失的部分；not_evidenced 的 evidence_refs 必须为空，并表达“资料未体现”。不得输出不存在的指标或用户经历。

证据通过 chunk_id、document_id、version 和 locator 定位。PDF 的 quote 必须来自对应页的实际提取文本，locator.page 为原 PDF 的真实页码；Markdown/TXT 的 quote 来自对应连续行区间。样例中数据库设计位于 project-demo.md 第 11 行，缓存说明位于第 15–16 行。转换成 PDF 后必须重新解析和确定页码，不沿用 Markdown 行号。实际分块可以包含更多上下文，只要引用准确，不能为匹配 fixture 强制伪造片段或 ID。

API 保留 snake_case 和统一 envelope。D 的内部 requirements/evidence_ids 由 B 根据本次检索证据映射为公开 matches/evidence_refs，API 附带 requirement 文本与 category，不能让前端自行猜测要求。完整示例是渲染契约，不是后端可以硬编码的固定输出。

## 联调顺序与完成证据

1. 负责人确认契约；A/B 确认 API，B/C/D 确认模块调用；认领各自任务。
2. A 跑通样例页面；B 保存真实资料/JD；C 独立验证索引/检索；D 验证拆解与输出。
3. B 连接 C 的入库、D 的分析和证据保存，A 切到真实 API，跑通完整流程。
4. 负责人执行下列 M1 验收，记录实际命令、截图、响应和已知问题。

- [ ] 有文本层的合成 PDF 简历与项目说明上传后完成真实解析/索引并显示 ready，生成的 ID 和版本可追溯；Markdown/TXT 上传仍可用。
- [ ] 多页 PDF 中已知片段可检索，引用展示正确原 PDF 页码和对应页文本；含空白页时页码不漂移，chunk 不跨页；文本引用仍显示真实行号。
- [ ] 超过 10 MiB、伪装格式、损坏、需要密码、无文本层及混合扫描内容的 PDF 有明确错误；接受后的解析失败可轮询到 failed，不能产生空索引或伪 ready。
- [ ] 从 JD 创建真实岗位和分析任务，状态最终明确成功或失败；刷新页面能继续查询。
- [ ] 三个要求输出合理状态，数据库和缓存的引用能回到原文；Docker 无证据时不编造。
- [ ] 资料未就绪返回 409；模型或解析失败能查看原因；错误的资源 ID 返回明确错误。
- [ ] 第二用户不能读取第一用户的文档、分析、任务或证据。
- [ ] 样例 JSON 和预期答案没有进入个人资料索引，真实界面不将 fixture 标成实际分析。
- [ ] 组员交付运行方法、配置变量、模块样例及已完成的自查；不要求负责人补做模块基础自查。
- [ ] C 提供可复现的 PDF 解析/页码自查，B 提供真实上传至索引链路记录，A 提供 PDF 上传/引用页面记录；未完成 PDF 链路不能关闭 M1。

公开开发样例只证明联调协议与基本流程，不能证明系统总体准确率。独立保留验收资料由负责人另行整理；所有真实效果数据来自实际运行。
