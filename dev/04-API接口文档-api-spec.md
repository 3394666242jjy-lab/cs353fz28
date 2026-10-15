# CareerPilot API 接口文档

状态：v0.1 契约草案，接口尚未实现。B 维护业务 API；A 使用样例响应开发；C/D 维护架构文档中的内部接口。

## 通用约定

- 基础路径 `/api/v1`，JSON 使用 UTF-8 和 snake_case。
- 注册及登录以外均使用 `Authorization: Bearer <access_token>`；令牌只放请求头，不放 URL。
- 服务端从令牌识别 user_id，不接受客户端通过 user_id 指定访问其他用户。
- 成功响应使用 `{"data": ..., "request_id": "..."}`；错误使用 `{"error":{"code":"...","message":"..."},"request_id":"..."}`。
- 列表采用 cursor、limit，默认 limit=20，最大 100；返回 items 和 next_cursor。
- 资源不存在、已删除或不属于用户时返回 404；过期分析不能直接生成新计划，返回 409 ANALYSIS_STALE。
- 异步创建返回 202 和 execution_id；前端轮询执行状态，不以 202 表示已完成。

## 认证

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /auth/register | email, password, display_name | 201：user_id；重复邮箱 409 |
| POST /auth/login | email, password | 200：access_token, token_type, expires_in |
| GET /me | 无 | 200：用户资料，不含密码哈希 |

密码策略、令牌有效期和限流参数在实现前由 B 与负责人确认；登出至少清理客户端令牌，服务端撤销方案在身份模块 Issue 中确定。

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
| GET /executions/{id} | 无 | 200：status、result_type、result_id、error、进度事件摘要 |
| GET /evidence/{chunk_id} | version | 200：原文片段、位置和来源；越权或已删除 404 |

POST /analyses 在服务端固定实际使用的文档版本。D 完成 JD 要求拆解后写入分析快照，更新岗位 requirements 时使用版本校验，不覆盖用户并发编辑。

分析结果样例（仅说明结构，不代表实际运行结果）：

```json
{
  "data": {
    "id": "analysis_demo",
    "job_id": "job_demo",
    "stale": false,
    "matches": [{
      "requirement_id": "req_01",
      "requirement": "具备数据库设计经验",
      "status": "supported",
      "evidence_refs": [{"chunk_id": "chunk_demo", "document_id": "doc_demo", "version": 1}],
      "explanation": "项目资料记录了数据库模型设计工作。",
      "missing_information": []
    }],
    "warnings": []
  },
  "request_id": "request_demo"
}
```

## 准备计划

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /plans | analysis_id, deadline, timezone, daily_minutes | 202：execution_id；成功任务的 result_id 指向 draft 计划 |
| GET /plans/{id} | 无 | 200：约束、draft_tasks 或正式 tasks、状态 |
| POST /plans/{id}/confirm | 无 | 200：confirmed 计划与任务；重复确认返回同一结果 |
| PATCH /plans/{plan_id}/tasks/{task_id} | status | 200：更新后的任务 |

deadline 使用 YYYY-MM-DD，timezone 为 IANA 时区标识，daily_minutes 为正整数且不超过 1440。服务端按本地日历计算可用天数，验证任务工时与每天安排。无法满足约束返回 422 PLAN_INFEASIBLE，并说明需调整的约束，不静默超时排程。

## 文本面试（P1）

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| POST /interviews | analysis_id | 201：interview_id, first_question |
| POST /interviews/{id}/answers | question_id, answer | 200：next_question 或完成状态；模型超时返回可重试错误 |
| POST /interviews/{id}/finish | 无 | 200：完成状态及 feedback |
| GET /interviews/{id} | 无 | 200：问答历史与已生成反馈 |

回答最长 8000 字符；问题编号校验当前轮次，重复提交同一 question_id 时不重复推进。最多 5 个主问题，每题最多 1 次追问。P1 具体反馈结构在实现前补充契约。

## 执行与错误

执行状态为 queued/running/succeeded/failed。仅 succeeded 提供有效 result_type/result_id；失败包含稳定错误码和适合用户阅读的原因。

| HTTP | 错误码示例 | 含义 |
| --- | --- | --- |
| 400/422 | INVALID_INPUT | 输入缺失或格式无效 |
| 401 | UNAUTHENTICATED | 未登录或令牌失效 |
| 404 | RESOURCE_NOT_FOUND | 不存在或不可访问 |
| 409 | VERSION_CONFLICT / DOCUMENT_NOT_READY / ANALYSIS_STALE | 当前状态或版本不支持操作 |
| 413/415 | FILE_TOO_LARGE / UNSUPPORTED_FILE_TYPE | 文件限制 |
| 429 | RATE_LIMITED | 请求过多，返回 Retry-After |
| 502/504 | MODEL_UNAVAILABLE / TOOL_TIMEOUT | 外部服务或执行超时 |

异步任务失败的错误写入 executions；轮询任务本身成功时 HTTP 仍为 200，前端必须读取任务 status，而不是只判断 HTTP。
