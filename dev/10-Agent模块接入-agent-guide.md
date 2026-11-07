# D：M1 Agent 模块与交接

状态：D 模块实现与离线自查；真实生成模型、B/C 全链路与多页 PDF 验收待完成。关联任务 #9，按 08 文档 v0.4 的 M1 范围交付。M2 的 B↔D 接入契约见文末专节（B 补充，对应 #26）。本文不声明整个项目已经可用。

## 已实现的流程

`backend/app/agent/pipeline.py` 使用真实 LangGraph `StateGraph`，不是手写流程冒充图：

```text
START → decompose_requirements → retrieve_evidence → assess_requirements → assemble_result → END
```

1. 生成模型从 JD 拆出要求，必须逐字来自 JD；代码分配 `req_001` 等本轮局部 ID。重复、捏造或超量拆解失败，不静默截断。
2. 每条要求调用白名单 `retrieve_evidence`。用户身份、文档版本及 top_k 来自业务上下文，模型不能改变，也不能选择其他工具。
3. 将要求与各自的实际证据交给生成模型，得到三类结构化判断。代码检查完整性、状态约束和证据 ID，不能引用其他要求的候选。
4. 从已经校验的证据组装公共 `AnalysisResult`，生成本轮结果与轨迹 ID，保留原始文档版本、引用位置和实际模型版本。

M1 缺失信息写入结果，不暂停追问；Checkpoint、人工确认、计划和模拟面试属于后续阶段。本图无写工具，不直接访问 MongoDB、队列、OSS 或 Qdrant。

## 复现离线演示

先按 09 文档安装 Python 3.12/3.13 和锁定依赖。仓库根目录执行：

```powershell
.\backend\.venv\Scripts\python.exe scripts/run_agent_demo.py --output storage/agent/offline-demo.json
```

此命令无需 Docker、账号或 API Key，不调用外部模型。它使用真实 LangGraph，但生成模型和检索均为显式合成替身，结果中的 `real_model`、`real_rag` 为 false，且包含警告。合成 Markdown 原文两行分别展示数据库设计、缓存但无测量；第三条 Docker 要求没有证据。正常得到 supported、partial、not_evidenced。

该演示只检查执行与契约，不证明模型分析质量、向量检索效果或 PDF 解析成功。PDF 页码透传与解析错误传播由单元测试覆盖；实际 PDF 上传和解析仍须 B/C 联调。

## 接入真实生成模型

生产工厂 `get_analysis_agent()` 只构造真实 HTTP 模型适配器；配置缺失报 `MODEL_NOT_CONFIGURED`，绝不自动退回合成响应。采用 Chat Completions 兼容协议和 JSON 输出，仍逐层进行 Pydantic/业务校验。

在根目录本机 `.env` 中设置以下项目，参考 `.env.example`，勿提交密钥：

| 配置 | 含义 |
| --- | --- |
| AGENT_BASE_URL | 控制台提供的兼容基础地址，包含 `/v1`，不包含 `/chat/completions` |
| AGENT_MODEL | 控制台显示的完整模型 ID；不能填写“免费的” |
| AGENT_API_KEY | 本机密钥；也可在交互演示中隐藏输入且不保存 |
| AGENT_RESPONSE_FORMAT | 默认 json_object；确认模型支持后可用 json_schema |
| AGENT_OUTPUT_TOKEN_PARAMETER | 默认 max_completion_tokens；老模型按官方文档显式设 max_tokens |
| AGENT_ENABLE_THINKING | 默认不传；模型要求非思考 JSON 模式时设 false |

阿里云百炼需要匹配业务空间/地域、模型和密钥。免费额度取决于账号及具体模型；本代码不会查询余额、保证免费或自动切换付费模型。调用前在控制台核对额度和费用设置。依据：[百炼兼容 API 官方文档](https://www.alibabacloud.com/help/zh/model-studio/qwen-api-via-openai-chat-completions)。模型支持范围与地址应以自己控制台为准。

确认后可运行：

```powershell
.\backend\.venv\Scripts\python.exe scripts/run_agent_demo.py --mode live-model --allow-live --output storage/agent/live-model-demo.json
```

也可用 `--base-url` 和 `--model` 指定非敏感配置，缺少密钥时在交互终端隐藏输入。该模式只验证“真实模型 + 合成检索”，仍不是 B/C 端到端联调。无 `--allow-live` 不发请求。错误不会打印服务端响应体或密钥；不要把敏感配置截图给他人。

## B 接入方式与职责

现有 `agent/contracts.py`、`rag/contracts.py` 与公共 Schema 未变：

```python
from app.agent.pipeline import get_analysis_agent

agent = get_analysis_agent()
result = await agent.run_analysis(
    user_id=authenticated_user_id,
    job=authorized_job,
    document_versions=pinned_document_versions,
    retriever=authorized_evidence_retriever,
)
```

以上变量由 B 的 Worker 提供。B 必须先鉴权、固定 JD/资料版本、检查 ready/删除状态；任务完成落库前再次检查版本及权限，资料变更时按生命周期规则拒绝或标记过期。D 的 `stale=False` 只表示本次输入快照，不代替当前数据库状态检查。

传入的 `EvidenceRetriever` 必须返回完整 `list[Evidence]`，包含可信原文 quote、document_id/version/chunk_id、filename/source_type 和 locator。C 的候选元数据需要 B 按当前用户从权威存储补齐；D 不以候选向量内容代替权威原文，不导入尚未合并的 B/C 服务。空列表仅表示检索成功但无候选；解析失败、资料未就绪和检索异常必须抛异常，不能吞错成空列表。

PDF locator 是原 PDF 页码，文本 locator 是原文行范围。D 只保留验证后的原值，不计算页码、不重新格式化 quote、不负责 PDF 解析。拒收公共知识作为个人经历、未请求文档/版本、仅元数据及相同 chunk_id 对应冲突原文。用户归属必须在可信 B/C 工具内部检查，Evidence 本身没有 owner 字段，D 无法独立验证实际数据库权限。

若 B 需要一次得到要求快照、完整被引用原文和轨迹，可选调用同参数的 `run_analysis_with_details()`，返回内部类型 `AnalysisRun`：

| 字段 | 用途 |
| --- | --- |
| result | 未改动的公共 AnalysisResult |
| graph_version | 本轮图版本 m1-analysis-graph-v1；提示词与模型版本位于 result |
| requirement_snapshot | 本轮 id/text/category，供 B 按已约定集合映射保存 |
| evidence | 实际被引用的完整 Evidence；不是全部候选或独立持久化承诺 |
| events | 阶段、耗时、累计工具/模型请求次数；不含隐藏推理 |
| usage | 服务返回的输入/输出 token 数量合计；缺失为 0，不等于已知零费用 |

这只是 D 的内部便利用法，不更改 API/集合，不要求 B 更改现有协议。B 按自己的业务 execution、任务 ID 和集合约定落库。D 生成的结果 ID/trace ID 是本轮唯一标识；跨任务重试/幂等仍由 B 管理。可信检索器可提供 `retrieval_config_version`；缺少时明确为 null 并警告，不编造版本。

## 输出约束与限制

| 状态 | 约束 |
| --- | --- |
| supported | 必须引用该要求实际检索到的片段，不能同时有待补充信息 |
| partial | 必须引用实际片段，并说明待补充信息 |
| not_evidenced | 无证据引用；表示当前资料未体现，不代表此人没有能力 |

系统提示将 JD/资料视为不可信数据，模型不能要求执行命令或选择任意工具。代码能验证引用来源、完整性和格式，不能数学证明自然语言结论正确；真实模型质量仍需人工核对和负责人评测。LangGraph 的用法参考[官方图 API](https://docs.langchain.com/oss/python/langgraph/graph-api)。

## 预算、失败与观测

默认每轮最多 8 条要求、8 次检索、每次 top_k=5；两次逻辑模型调用，每次最多 2 次 HTTP 尝试（本轮最多 4 次）。单次逻辑模型调用与单次工具调用各不超过 30 秒，总图不超过 180 秒，模型输出上限 4096 token，证据上下文上限 80000 字符，响应体上限 2 MiB。这些是执行上限，不是金额预算；部分模型的 max_tokens 不含思考 token，需选择支持的限额参数和非思考模式。错误不自动触发重新生成整图。

仅网络错误、408/429/5xx 在上限内短暂重试；认证、请求参数、JSON/引用校验失败不重试。总图超时、工具超时和模型超时分别报错。禁止把失败转换成成功的 not_evidenced。

`AgentError.as_dict()` 提供 code/message/retryable/trace_id，供 B 映射任务失败状态；不要直接把异常链或原始模型响应返回前端。常见 code：MODEL_NOT_CONFIGURED、MODEL_AUTH_FAILED、MODEL_REQUEST_REJECTED、MODEL_RATE_LIMITED、MODEL_UNAVAILABLE、MODEL_TIMEOUT、MODEL_OUTPUT_INVALID、RETRIEVAL_FAILED、RETRIEVAL_TIMEOUT、DOCUMENT_NOT_READY、DOCUMENT_PARSE_FAILED、EVIDENCE_INVALID、BUDGET_EXCEEDED、ANALYSIS_TIMEOUT、ANALYSIS_FAILED。

轨迹仅记录阶段和调用数量；图执行期间用 LangSmith tracing_context 显式关闭继承的外部自动追踪，不修改进程环境，避免环境开关意外上传资料。LangSmith 为 LangGraph 现有间接依赖，此处显式声明仅用于禁用追踪，并非启用外部观测。引用和快照可能含个人数据，B 按业务授权与生命周期保存；storage/agent 的本机演示报告被 Git 忽略。真实推理仍需将 JD/证据发送给配置的生成模型服务。

## 自查与未完成验收

根目录运行（离线，无外部模型费用）：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -q
.\backend\.venv\Scripts\ruff.exe check --config backend/pyproject.toml backend/app backend/tests scripts
.\backend\.venv\Scripts\ruff.exe format --check --config backend/pyproject.toml backend/app backend/tests scripts
```

覆盖三类状态、真实图运行、并发隔离、文本/PDF locator 透传、假引用/跨要求引用、版本与来源拒收、解析/就绪错误传播、预算和超时、HTTP 重试边界、认证失败、结构化输出校验。HTTP 模型测试使用 MockTransport，不表示已连通百炼。

剩余联调：配置并实测具体模型；B 提供带用户/版本检查的 EvidenceRetriever，C 提供实际索引检索；多页含空白页 PDF 上传后从原文到分析再到前端核对引用，并验证解析失败与版本更新/删除。现阶段不修改 API 的 501 或能力开关来假装全链路可用。负责人验收前 Issue #9 保持未完成。

代码、提示词、测试与本文使用 Codex 辅助编写，提交成员需理解、核验和维护。PR 记录实际执行的检查与限制；不得把离线演示当成真实模型效果或 PDF 全链路验收。

## M2 接入：计划生成与持久化恢复（B↔D 契约）

> 本节由 B（Issue #24）补充，对应 Issue #26。B 的 Worker（`backend/app/worker.py` 的 `run_plan` / `run_resume`）通过 outbox 触发后，惰性导入 D 的两个异步入口；以下签名以 B 侧实现为准，PR #28 即为参照实现。

### D 需要提供的两个入口

**1. `app.agent.plan.generate_plan_draft`（计划草稿）**

```python
async def generate_plan_draft(
    *, user_id: str,
    plan: dict,       # plans 集合行：id、analysis_id、execution_id、deadline（ISO 日期字符串）、timezone、daily_minutes
    analysis: dict,   # analyses 集合行（status 已校验为 ready）：requirement_snapshot、matches、document_versions
    settings: Settings,
) -> PlanDraftOutcome
```

返回对象字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `draft_tasks` | list | 必填、不得为空。每项含 `title`（非空）、`requirement_ids`（list[str]）、`scheduled_date`（date 或 ISO 字符串）、`estimated_minutes`（int ≥ 1）、`source_refs`（可空；每项 `{document_id?, version?, chunk_id?}`） |
| `interrupt_id` | str | 可选；缺省时 B 生成 |
| `checkpoint_id` | str | 可选；缺省时 B 生成 |

B 收到结果后保存草稿并把执行停在 `waiting_for_input`（`pending_input.kind="plan_confirmation"`，附 `plan_id`）。草稿为空或保存冲突按生成失败处理（对外固定文案，不暴露异常细节）。

**2. `app.agent.resume.resume_execution`（图恢复）**

```python
async def resume_execution(
    *, user_id: str,
    execution_id: str,
    resume_input: dict,  # execution_inputs 集合行：kind（clarification/plan_confirmation）、
                         # answer {text, continue_without_evidence, document_ids}、document_versions、
                         # interrupt_id、checkpoint_id
    settings: Settings,
) -> ResumeOutcome
```

返回对象字段：

| status | 还需携带 |
| --- | --- |
| `"waiting_for_input"` | `pending_input`：`{kind, interrupt_id, checkpoint_id, question?, plan_id?}` |
| `"succeeded"` | `result_type`（`analysis`/`plan`/`document`）与 `result_id` |
| `"failed"` | `error_message`（可直接给用户看的一句话） |

### 双方不变式

- **B 拥有全部持久化**：D 的入口不直接写 MongoDB、队列或存储；状态转换、事件流（SSE）、输入记录与投递都由 B 完成。D 若需跨暂停保留图状态，用 execution_id/interrupt_id/checkpoint_id 自行存 checkpoint（文件或自有集合），B 调用前已校验 interrupt/checkpoint 与用户归属。
- **至少一次调用**：outbox 保证消息至少到达一次，重复投递由 B 的状态守卫拦截，但 D 的恢复入口仍应按幂等实现（同 checkpoint 的重复恢复不重复产生副作用）。
- **确认后不重写**：计划确认后的恢复不得重新生成或改写已确认内容。
- **错误不伪装**：入口抛异常或返回 failed 均可，B 统一映射为任务失败（`PLAN_GENERATION_FAILED` / `RESUME_FAILED`），文案由 B 固定，D 不返回堆栈或原始模型响应。

### 联调步骤

1. D 先交付合成替身（不调用真实模型）使 `careerpilot.run_plan` / `careerpilot.resume_execution` 全链路可跑，B 侧已有对应单测可参照（`backend/tests/test_worker_m2.py`）。
2. 再接入真实模型与真实检索，按 #26 验收清单逐条过。
