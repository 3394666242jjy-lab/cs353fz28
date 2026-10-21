# CareerPilot API 接口文档

状态：v0.3 契约候选版。M1 路由、Pydantic 模型与 OpenAPI 已建立；B 已实现本地 POST /auth/login 和 GET /me，其余有效业务请求仍返回 501 NOT_IMPLEMENTED。运行方法见 [09 本地启动指南](09-本地开发启动-local-setup.md)。B 维护业务 API；A 使用样例开发；C/D 维护检索和 Agent 内部契约。SSE 及人工恢复为 M2。

## M1 必要接口与响应字段

完整产品接口保留在下文；M1 仅实现以下子集。文件仅支持 UTF-8 Markdown/TXT，PDF 在 M2；身份使用本地种子账号，不要求首轮开放注册。认证、用户隔离和标准错误格式仍生效。

| 方法与路径 | 本轮请求 | 本轮响应约定（均使用统一 envelope） |
| --- | --- | --- |
| POST /auth/login、GET /me | 登录信息或有效令牌 | 返回令牌或本人资料，不返回密码哈希 |
| POST /documents | multipart file、kind=resume/project | 202：document_id、execution_id、status=processing |
| GET /documents、GET /documents/{id} | 列表分页或文档 ID | 列表用 items/next_cursor；单资源含 id、kind、filename、current_version、status、error（可空） |
| POST /jobs、GET /jobs/{id} | title、raw_jd、company（可选） | 创建返回 201：job_id、version；查询返回 id、title、raw_jd、version |
| POST /analyses | job_id、document_ids | 202：execution_id；资料未就绪返回 409 |
| GET /executions/{id} | 执行 ID | id、type、status、state_version、result_type、result_id、error；pending_input 和 latest_event_id 本轮可为 null |
| GET /analyses/{id} | 分析 ID | id、job_id、job_version、document_versions、stale、matches、warnings；版本及 trace 元数据按实际能力返回 |
| GET /evidence/{chunk_id} | version | chunk_id、document_id、version、filename、source_type、locator、quote |

`GET /analyses/{id}` 的每条 match 必须有 requirement_id、requirement、category、status、evidence_refs、explanation 和 missing_information。category 为 required/preferred；status 为 supported/partial/not_evidenced。前两类必须有原文引用，not_evidenced 的 evidence_refs 为空。每条引用包含 chunk_id、document_id、version。

M1 用轮询查询 executions，终态停止；缺少证据时直接返回分析中的 missing_information，不进入 waiting_for_input。D 拆解的要求保存在分析快照，B 组装公开响应，不覆盖原始 JD。fixture 里的 ID 和版本标签仅为渲染样例，真实路径使用服务端生成 ID 和实际运行元数据。

以下统一响应包裹保持不变：成功为 data/request_id，失败为 error/request_id。完整样例见 [请求描述](../examples/requests.json)、[分析响应](../examples/analysis-response.json)、[证据响应映射](../examples/evidence-responses.json) 和 [执行快照](../examples/execution-response.json)。evidence-responses.json 是多请求 fixture 映射，每个 API 只返回其中对应的单资源 envelope。

## 通用约定

- 基础路径 `/api/v1`，JSON 使用 UTF-8 和 snake_case。
- 注册及登录以外均使用 `Authorization: Bearer <access_token>`；令牌只放请求头，不放 URL。
- 服务端从令牌识别 user_id，不接受客户端通过 user_id 指定访问其他用户。
- 成功响应使用 `{"data": ..., "request_id": "..."}`；错误使用 `{"error":{"code":"...","message":"..."},"request_id":"..."}`。
- 列表采用 cursor、limit，默认 limit=20，最大 100；返回 items 和 next_cursor。
- 资源不存在、已删除或不属于用户时返回 404；过期分析不能直接生成新计划，返回 409 ANALYSIS_STALE。
- 异步创建返回 202 和 execution_id，不以 202 或 Celery 消息状态表示业务完成；M1 使用轮询，M2 接入 SSE 后保留查询作为恢复后备。

## 认证

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /auth/register | email, password, display_name | 201：user_id；重复邮箱 409 |
| POST /auth/login | email, password | 200：access_token, token_type, expires_in |
| GET /me | 无 | 200：用户资料，不含密码哈希 |

当前本地实现：种子密码为 12–1024 字符，原样核对，不裁剪密码空白；邮箱去首尾空白并转小写。令牌为不透明 Bearer 字符串，默认 3600 秒过期，由 ACCESS_TOKEN_TTL_SECONDS 配置（60–86400 秒）。后端只保存令牌摘要；客户端不要解析或将其放入 URL。

错误邮箱、错误密码、缺失/无效/过期令牌统一返回 401 UNAUTHORIZED 和 WWW-Authenticate: Bearer；数据库不可用返回脱敏的 503 DATABASE_UNAVAILABLE。成功响应沿用既有 Envelope、TokenResult、UserResult。Swagger 文档可通过 Authorize 输入原始令牌测试 GET /me。

本地密码及会话策略随 B 的 PR 交负责人确认；公开注册、登出撤销、登录限流和生产身份策略尚未实现。当前不得将登录模块视为生产发布完成。

## 个人资料

| 方法与路径 | 请求或参数 | 响应 |
| --- | --- | --- |
| POST /documents | multipart/form-data：file、kind=resume/project | 202：document_id, execution_id, status=processing |
| GET /documents | cursor, limit | 200：文件元数据列表 |
| GET /documents/{id} | 路径 id | 200：文件信息、版本、状态、允许查看的提取文本 |
| PUT /documents/{id}/text | extracted_text, expected_version | 202：新 version、execution_id；版本冲突 409 |
| POST /documents/{id}/reindex | expected_version | 202：execution_id；已有任务时返回同一任务 |
| DELETE /documents/{id} | 无 | 202：status=deleted, purge_status=pending；立即阻断后续访问 |

文件仅支持有文本层 PDF 和 UTF-8 TXT/Markdown，最大 10 MiB；413 为过大，415 为不支持，422 为无法解析。删除和版本更新后，相关分析应显示 stale 或来源不可用。

## 岗位与分析

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /jobs | title, company（可选）, raw_jd | 201：job_id, version；初始 requirements 可为空 |
| GET /jobs | cursor, limit | 200：岗位列表 |
| GET /jobs/{id} | 无 | 200：原始 JD、要求及版本 |
| PATCH /jobs/{id} | raw_jd 或 requirements，expected_version | 200：新版本；旧分析标记 stale |
| POST /analyses | job_id, document_ids | 202：execution_id；资料未就绪 409 DOCUMENT_NOT_READY |
| GET /analyses/{id} | 无 | 200：匹配结果、证据引用、warnings、stale |
| GET /executions/{id} | 无 | 200：status、state_version、result_type、result_id、pending_input、error、latest_event_id |
| GET /executions/{id}/events | after_event_id（可选） | 200：鉴权 SSE；回放后持续推送本任务业务事件 |
| POST /executions/{id}/resume | interrupt_id, checkpoint_id, answer | 202：execution_id；只恢复资料追问，计划确认走独立接口 |
| GET /evidence/{chunk_id} | version | 200：原文片段、位置和来源；越权或已删除 404 |

POST /analyses 在服务端固定实际使用的文档版本。D 完成 JD 要求拆解后写入分析快照；M1 不自动更新岗位要求。后续如需写回岗位 requirements，使用版本校验，不覆盖用户并发编辑。

## 执行事件与补充信息（M2）

`GET /executions/{id}/events` 使用 Authorization 请求头，响应 Content-Type 为 text/event-stream，禁止响应缓存。前端使用 fetch 读取流，不将令牌放 URL。after_event_id 或 Last-Event-ID 指定最后成功应用的事件；两者同时传入且不一致返回 400。事件游标须属于当前任务，重复事件按 id 去重。

事件示例（只说明协议，不代表实际结果）：

```text
id: evt_demo_4
event: stage
data: {"execution_id":"exec_demo","status":"running","stage":"retrieving_evidence"}

id: evt_demo_5
event: waiting_for_input
data: {"execution_id":"exec_demo","status":"waiting_for_input","kind":"clarification","interrupt_id":"int_demo","checkpoint_id":"cp_demo","question":"是否有数据库优化的项目记录可补充？"}

```

事件类型与 03 文档一致：stage、waiting_for_input、completed、failed。等待输入或终态事件发出后服务器可关闭流；客户端不能对终态无限重连。心跳使用 SSE 注释，P0 不发送 token 级内容；最终结果通过 resource API 查询。

SSE 建连与读取事件窗口时必须避免“先回放、后订阅”的间隙丢失。连接重建先从持久化事件游标继续，必要时再次读快照；旧游标已清理时建连前返回 410 EVENT_HISTORY_EXPIRED，前端查询 execution 快照和 latest_event_id 后重连。无历史但任务已终结时查询结果资源，不必继续订阅。

resume 仅接受本用户 execution 当前 pending_input.kind=clarification。answer 结构为 `{"text":"用户补充","continue_without_evidence":false,"document_ids":[]}`；text 最长 8000 字符，明确选择 continue_without_evidence 或提供新文档时允许空文本。document_ids 可选，默认空数组；需要新资料时先上传并等索引就绪，再提供文档 ID，由服务端核对用户和就绪状态并固定恢复输入版本。用户自由文本属于待核实补充，不自动升级为有文档证据的经历。

服务端匹配 interrupt_id、checkpoint_id 和 state_version，验证来源仍可使用，将恢复输入及 dispatch_events 持久化，再返回 202。前端不能传任意 thread_id 或任意图节点命令。同一恢复请求重复提交返回同一接受结果；同一 interrupt 使用不同回答重放时返回 409 INPUT_CONFLICT；旧 Checkpoint 返回 409 STALE_CHECKPOINT。

完整分析结果统一维护在 [analysis-response.json](../examples/analysis-response.json)，覆盖三种匹配状态和实际原文定位，避免文档内另存一份字段不同的样例。SSE 协议场景见 [execution-events.txt](../examples/execution-events.txt)，只供 M2 提前对齐，不代表接口已经实现。

## 准备计划（M2）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /plans | analysis_id, deadline, timezone, daily_minutes | 202：execution_id；草稿生成后由 waiting_for_input 快照的 result_id 查看 |
| GET /plans/{id} | 无 | 200：约束、draft_tasks 或正式 tasks、状态 |
| POST /plans/{id}/confirm | 无 | 200：confirmed 计划、任务与 execution_id；幂等保存并记录图恢复指令，重复确认返回同一结果 |
| PATCH /plans/{plan_id}/tasks/{task_id} | status | 200：更新后的任务 |

deadline 使用 YYYY-MM-DD，timezone 为 IANA 时区标识，daily_minutes 为正整数且不超过 1440。服务端按本地日历计算可用天数，验证任务工时与每天安排。无法满足约束返回 422 PLAN_INFEASIBLE，并说明需调整的约束，不静默超时排程。

计划草稿生成后，execution 为 waiting_for_input，pending_input.kind=plan_confirmation，result_type=plan，result_id 指向 draft 计划。用户确认前不创建正式任务。confirm 重新检查权限、来源版本及安排，在同一业务事务中保存任务和恢复指令；200 表示正式计划已保存，图恢复完成可稍后通过 execution 查询。重复请求不重复创建任务或恢复指令。模型在确认后的恢复节点不再次生成或改写已确认计划。

## 文本面试（P1 / M3）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /interviews | analysis_id | 201：interview_id, first_question |
| POST /interviews/{id}/answers | question_id, answer | 200：next_question 或完成状态；模型超时返回可重试错误 |
| POST /interviews/{id}/finish | 无 | 200：完成状态及 feedback |
| GET /interviews/{id} | 无 | 200：问答历史与已生成反馈 |

回答最长 8000 字符；问题编号校验当前轮次，重复提交同一 question_id 时不重复推进。最多 5 个主问题，每题最多 1 次追问。P1 具体反馈结构在实现前补充契约。

## 执行与错误

执行状态为 queued/running/waiting_for_input/succeeded/failed。succeeded 提供最终 result_type/result_id；waiting_for_input 提供 pending_input，计划确认场景还提供已保存的 draft 计划 ID；其他非终态不宣称有最终结果。失败包含稳定错误码和适合用户阅读的原因。

| HTTP | 错误码示例 | 含义 |
| --- | --- | --- |
| 400/422 | INVALID_INPUT | 输入缺失或格式无效 |
| 401 | UNAUTHENTICATED | 未登录或令牌失效 |
| 404 | RESOURCE_NOT_FOUND | 不存在或不可访问 |
| 409 | VERSION_CONFLICT / DOCUMENT_NOT_READY / ANALYSIS_STALE / STALE_CHECKPOINT / INPUT_CONFLICT | 当前状态、版本或恢复请求冲突 |
| 410 | EVENT_HISTORY_EXPIRED | SSE 历史游标已超出保留窗口，需重新读取快照 |
| 413/415 | FILE_TOO_LARGE / UNSUPPORTED_FILE_TYPE | 文件限制 |
| 429 | RATE_LIMITED | 请求过多，返回 Retry-After |
| 502/504 | MODEL_UNAVAILABLE / TOOL_TIMEOUT | 外部服务或执行超时 |

异步任务失败的错误写入 executions；轮询任务本身成功时 HTTP 仍为 200，前端必须读取任务 status，而不是只判断 HTTP。

数据库、Redis、OSS、Qdrant 和模型服务由服务端访问，前端不接收其凭据。实际部署通过 Nginx 同域反向代理提供 `/api/v1`；健康检查路径由部署指南定义，不在 SSE 里暴露内部连接详情。
