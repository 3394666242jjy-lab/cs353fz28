# CareerPilot 部署指南

状态：v0.3 分阶段实施与阿里云 ECS 部署设计草案。负责人统一负责最终测试、环境配置、正式部署及回滚。应用、Compose 文件和 GitHub Actions 尚未搭建；本文描述目标方案，不代表已购买资源、部署或启用流水线。具体命令在脚手架阶段补齐。

## 环境与交付责任

| 环境 | 用途 | 数据 |
| --- | --- | --- |
| local | 四位成员开发与联调 | 合成简历、公开资料和模拟 JD |
| staging | 负责人端到端验收、权限和恢复验证 | 独立测试账号与可清理数据 |
| production/demo | 课程展示与获准使用 | 演示使用合成或明确获准的个人资料 |

A 提供前端构建和 SSE 接入说明；B 提供 API、Celery Worker、Outbox 分发器、MongoDB 初始化、队列恢复与健康检查；C 提供 Qdrant、解析、Embedding、Rerank 和索引重建说明；D 提供 LangGraph Checkpoint、模型、提示词和工具预算配置。各成员提供模块的容器构建与配置需求，最终 Compose、流水线及云端发布由负责人整合。

## 本地开发与云服务器的关系

- 阿里云 ECS：最终运行应用的 Linux 云服务器。
- Docker：将应用及依赖打包成镜像，在容器中运行。
- Docker Compose：管理多个服务及其网络、配置与数据卷，本地和 ECS 使用对应环境配置。
- GitHub Actions：执行检查和构建镜像；正式发布由负责人手动触发，不随任意推送自动覆盖演示环境。

完整交付路径：本地开发 → 工作分支与 PR 检查 → 审查合并 → main 对应镜像构建 → 负责人确认发布 → ECS 拉取指定镜像并更新服务 → 验收或回滚。阶段性未搭建的能力必须标记，不能只凭文档宣称已实现。

## 云端拓扑

| 部署位置 | 组件 | 说明 |
| --- | --- | --- |
| 一台阿里云 ECS | Nginx、API、Celery Worker、Outbox 分发器、Redis、Qdrant | 使用 Compose 管理；最初按单机演示设计，不承诺高可用 |
| Nginx 容器 | React 构建出的静态文件、HTTPS、API 和 SSE 反向代理 | 浏览器使用同域 `/api/v1`；前端可以通过多阶段镜像构建后交给 Nginx 提供 |
| 托管 MongoDB 副本集 | 业务记录、待分发指令、事件、LangGraph Checkpoint | 优先验证与 ECS 同地域的阿里云 MongoDB；需要事务和 Checkpoint 驱动兼容性 |
| 阿里云 OSS 私有 Bucket | 原始简历和项目文件、解析文本及获准备份 | 不将原始文件作为公开静态资源；下载需鉴权或短时签名 |
| 外部模型服务 | 生成、Embedding、Rerank API | 初期不在 ECS 上部署大模型或要求 GPU，验证从云端访问服务的可用性 |

地域、规格、带宽、磁盘、模型配额及费用由负责人根据原型测量确认，不在此预填购买方案。staging 与 demo 应分离数据及凭据；预算受限时可以复用一台 ECS 的不同 Compose 项目，但仍有共同单机故障风险。

## 网络与持久化

- 公开入口为 Nginx HTTPS；SSH 仅对维护来源开放。API 由反向代理访问，MongoDB、Redis、Qdrant 不直接暴露公网服务端口。
- ECS、托管数据库和 OSS 尽量使用同地域及可用的私网连接；应用账号采用所需最小权限。
- Qdrant 索引及 Redis 数据使用持久化数据卷。Redis 持久化与消息恢复方式须配置并演练；可靠性仍依赖 MongoDB 状态、Outbox 重投和幂等处理，不能只依赖 Redis。
- Nginx 对 SSE 路由关闭响应缓冲，配置心跳和合适的读超时；服务重启断流后由前端按事件 ID 恢复。
- 证书、服务端配置及密钥独立于镜像；更新服务不删除数据卷，不将原文件留在容器临时层。

## 配置清单

以下仅定义变量用途，不包含真实值；实际 `.env.example` 在脚手架阶段添加。

| 变量 | 用途 | 是否允许进入前端 |
| --- | --- | --- |
| APP_ENV | local/staging/production | 可按需暴露环境名称 |
| PUBLIC_API_BASE_URL | 业务 API 地址 | 是 |
| MONGODB_URI / MONGODB_DATABASE | 业务数据库连接和库名 | 否 |
| AUTH_SIGNING_KEY | 认证签名密钥 | 否 |
| OBJECT_STORAGE_ENDPOINT / BUCKET | 私有对象存储地址和桶 | 否 |
| OBJECT_STORAGE_ACCESS_KEY / SECRET_KEY | 文件访问凭据 | 否 |
| VECTOR_STORE_URL / VECTOR_STORE_API_KEY | 向量索引及凭据 | 否 |
| LLM_BASE_URL / LLM_API_KEY / LLM_MODEL | 生成模型适配器 | 否 |
| EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL / EMBEDDING_DIMENSION | 嵌入适配器及索引维度 | 否 |
| RERANK_BASE_URL / RERANK_API_KEY / RERANK_MODEL | 候选片段重排序，启用时配置 | 否 |
| RETRIEVAL_MODE / RETRIEVAL_CONFIG_VERSION | dense/hybrid/hybrid_rerank 与检索配置版本 | 否 |
| CELERY_BROKER_URL | Redis 消息代理地址；业务结果保存在 MongoDB，不以 Celery 结果作为权威状态 | 否 |
| CHECKPOINT_DATABASE | MongoDB 中独立的 LangGraph 状态库，不接受用户指定线程或库名 | 否 |
| CELERY_VISIBILITY_TIMEOUT_SECONDS | 根据任务最大运行时间配置 Redis 重投时限并验证 | 否 |
| OUTBOX_RECONCILE_INTERVAL_SECONDS | 未分发指令和失效运行租约的恢复检查间隔 | 否 |
| SSE_HEARTBEAT_SECONDS | 事件流心跳间隔，必须小于代理空闲断开时限 | 否 |
| TRACE_EXPORT_ENABLED | 外部观测上传开关，默认关闭；启用前验证脱敏和访问控制 | 否 |
| LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY | 可选外部追踪配置；项目中均保持服务端使用 | 否 |
| AGENT_MAX_STEPS / TOOL_TIMEOUT_SECONDS / TASK_TIMEOUT_SECONDS | 执行预算 | 否 |
| CORS_ALLOWED_ORIGINS | 明确允许的前端域名 | 否 |

生产密钥使用托管密钥配置，不放仓库、截图或日志。MongoDB、向量索引与对象存储分别最小权限配置；不同环境不共享用户数据。

## 首次部署顺序

1. 确认发布版本已获负责人认可，记录 commit SHA；日常通过已审查合并版本发布。
2. 创建 ECS、托管 MongoDB 和私有 OSS，配置账号、网络及备份；验证事务和 MongoDBSaver 兼容性。
3. 安装 Docker/Compose，配置镜像仓库访问、固定 SHA 镜像标签、数据卷和环境变量。
4. 初始化 MongoDB 集合与索引、Qdrant 向量/稀疏字段及 Checkpoint 存储；记录嵌入维度。
5. 启动 Redis、API、Outbox 分发器和 Worker，验证任务投递、重复执行保护及故障恢复。
6. 部署 Nginx 和前端，配置同域 API、HTTPS、上传限制及 SSE 代理。
7. 导入有来源的公共知识与合成资料，完成检索、用户确认暂停/恢复和业务验收。
8. 保存线上 URL、发布 SHA、配置与模型版本、资源规格、评测结果及已知问题。

## GitHub Actions 目标流程

| 阶段 | 触发 | 操作 | 是否更新 ECS |
| --- | --- | --- | --- |
| PR 检查 | pull_request | 文档/格式、类型与模块检查；应用存在后加入构建及受影响验证 | 否 |
| 镜像构建 | main push | 构建前端及服务端镜像，标记精确 commit SHA，推送配置好的镜像仓库 | 否 |
| 正式发布 | 负责人手动触发 workflow_dispatch | 验证所选 SHA 来自获准的 main 历史，登录目标 ECS，拉取镜像并更新 Compose | 是 |

镜像仓库可选择阿里云 ACR，配置访问权限和配额后再启用。部署凭据只在受保护的发布环境使用；来自不受信任 PR 的代码不能获得生产凭据。若仓库计划不支持环境审批，保留负责人手动发布，不依赖不存在的审批功能。

## 分阶段实现

1. M1 主线联调：文本向量检索、LangGraph 基本流程、真实 API 与引用页面，最小 Celery/Redis 分发及 MongoDB 状态轮询；只用合成资料与本地私有文件适配器。资料、接口和任务见 [M1 开工计划](08-M1开工计划-kickoff-plan.md)。
2. M2 完善 P0：PDF、更新与删除、准备计划确认；加入 Outbox/重启恢复、SSE、Checkpoint 暂停/继续，比较混合检索及 Rerank。
3. M3 可选扩展：任务看板、文本模拟面试，按团队进度安排。
4. 云端交付：负责人根据完整 P0 就绪情况整合 Compose、Actions、OSS、HTTPS 与备份，执行 ECS 发布和回滚演练；不要求 M1 前购买云资源，也不依赖所有 P1 扩展完成。

M1 只承诺本地开发可运行，不承诺完整队列故障补偿。任务投递失败或无法恢复时必须明确失败，不永久显示 queued/running。开发中的同步模块调用仅用于独立样例，整链验收使用真实任务与检索；最终 P0 云端发布仍须达到本文的持久化、权限、恢复和事件接口要求。

## 发布前验收

- 上传 → 索引 → JD → 分析引用 → 计划预览 → 确认 → 进度保存全部可用。
- 两个不同用户不能访问彼此的文件、片段、分析、任务和面试。
- 删除及更新资料后，不会继续检索被删除或错误版本的片段。
- 模型超时、Worker 重启、重复确认和文件解析失败都有明确状态。
- Redis 短时不可用时待分发指令保留，恢复后可投递；重复消息不重复创建片段、计划任务或用户可见结果。
- LangGraph 等待用户时不占用 Worker；服务重启后能够确认计划或继续补充资料。
- SSE 重连可回放业务事件，缺少历史事件时可通过快照恢复，不重复拼接输出。
- 前端包及日志不含密钥；原始文件不可匿名访问。
- 证书有效，浏览器可以访问 HTTPS，演示录屏使用稳定数据。

## 健康与观察

计划提供 GET /health/live 和 GET /health/ready，不包含业务数据。live 表示进程存活，ready 检查必要服务连通性；不为每次健康检查调用付费模型。负责人观察错误率、任务耗时、队列积压、索引失败和模型用量；不在普通日志记录简历全文。

## 回滚与恢复

保存上一版镜像 SHA、Compose、配置与 LangGraph 图/Checkpoint 格式版本。回滚先暂停新任务、处理或检查在途任务，再切换镜像；暂停中的旧图线程须使用兼容版本恢复，或由用户明确重建任务，不能盲目加载不兼容 Checkpoint。数据库变更先验证向后兼容性，不通过删除数据库回滚。

托管 MongoDB 备份保存权威业务和 Checkpoint；OSS 保留获准的原文件版本；Qdrant 可按 MongoDB 和 OSS 重建，或恢复兼容索引快照。Redis/队列恢复后用 Outbox 与 executions 核对待办，不将队列恢复等同于业务状态恢复。数据卷和备份恢复步骤必须实际演练。

## 待补充内容

脚手架创建后补齐：精确运行时版本、安装及启动命令、构建命令、镜像名、健康检查实现、初始化脚本、云资源清单、成本预算、备份保留期与实测恢复步骤。未验证的步骤必须标记待验证。

官方参考：[阿里云 ECS 上安装 Docker 与 Compose](https://help.aliyun.com/zh/ecs/user-guide/install-and-use-docker)、[Compose 单服务器部署](https://docs.docker.com/compose/how-tos/production/)。
