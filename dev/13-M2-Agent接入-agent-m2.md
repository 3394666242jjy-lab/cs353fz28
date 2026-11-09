# D / 冯威：M2 Agent 开发记录

**当前完成情况与复现方式见 [14 M2 Agent 验收](14-M2-Agent验收-agent-acceptance.md)。以下保留逐轮开发过程记录，其中“待开发”描述的是当时状态。**

关联 Issue #26。2026-11-09 从 main 9eb06dff5904b2341b12862e2a4fc0f10dc95705 创建工作分支 codex/26-m2-agent。

## 当前交付：计划草稿与计划确认快照恢复

`app.agent.plan.generate_plan_draft(*, user_id, plan, analysis, settings)` 对接 B 的 PR #28 草稿生成入口。返回对象的 draft_tasks 为普通 JSON 字典列表，日期为 ISO 字符串，引用为普通字典，供 B Worker 保存。B 负责写草稿和进入等待确认状态。本模块不保存或创建正式任务。

输入校验包括分析 ready 且未过期、分析 ID、可用的用户归属字段、计划尚未确认、有效时区、未过去的截止日期和每日分钟数。B 仍必须进行当前权限、资料版本和删除状态复核。

模型结果校验包括非空任务、已知要求 ID、禁止重复要求 ID、合法日期、同一天任务总分钟数、仅引用关联要求已有的原文引用。返回原候选或明确失败，不偷偷挪动日期、截断任务或替模型编造引用。生成使用现有真实模型适配器，模型配置缺失不会退回合成响应。

## 第一轮检查（计划草稿）

- 新增 `backend/tests/test_agent_plan.py`：17 项离线检查通过，使用明确模型替身。
- Ruff 格式与检查通过。
- 首轮完整后端检查：310 passed、8 skipped（随后修改了草稿 JSON 返回格式，又复跑上述 17 项并通过）。
- 未调用真实模型生成 M2 计划；未执行真实数据库、B Worker 或浏览器计划联调。不把模块测试说成 M2 验收通过。

检查命令（从仓库根目录）：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_agent_plan.py -q
.\backend\.venv\Scripts\ruff.exe check --config backend/pyproject.toml backend/app/agent/plan.py backend/tests/test_agent_plan.py
```

## 本轮新增：计划确认恢复

- `agent/checkpoints.py`：SQLite 保存 D 的计划快照。用户与执行编号绑定；生成前原子认领，保存实际草稿及稳定 interrupt/checkpoint ID；重新打开或新进程可恢复。
- `agent/resume.py`：实现 B 约定的异步入口，仅支持计划确认。查询 B 已确认的权威计划，核对归属、执行 ID 和完整草稿，再完成快照。不生成正式任务，不重新调用模型。
- 同一确认可重复或并发执行，返回同一计划 ID；旧编号、其他用户、确认内容变化均明确拒绝。
- 模型调用前保存保守预留的请求次数及运行秒数，恢复不重置。当前确认阶段不调用模型或工具；后续分析追问阶段的累计预算仍需另外实现。
- 模型调用中途退出会保留未完成的认领，明确拒绝自动重跑，避免重复计费。当前需要调查失败原因后创建新业务执行，不承诺任意生成节点崩溃后的自动续跑。
- 这是 D 的单机步骤快照，不是已接通的 LangGraph 原生 checkpointer。多机共享存储、快照清理与完整暂停图仍待集成。
- SQLite 文件位于 `settings.local_storage_dir/agent/checkpoints.sqlite3`；与私有开发数据同目录，不能提交。B 的 MongoDB 业务状态仍是对外权威。
- B 的 PR #28 尚未合入当前 main；生产恢复入口惰性导入其计划查询，缺失时明确失败，不使用测试确认记录替代。

## 本轮验证结果

- 计划和快照专项测试共 29 项，包含全新 Python 子进程读取并恢复、8 次并发重复确认、错误用户、旧编号、变更草稿及非布尔确认拒绝。
- 完整后端：322 passed、8 skipped，耗时 66.24 秒；跳过项不算真实依赖验证通过。
- 新代码与演示脚本 Ruff 检查、格式通过。
- 真实模型演示生成 4 项任务；磁盘恢复及重复确认成功，预算记录未重置。
- 本机运行记录：`storage/agent/m2-plan-1f9f15ecc9df41aabd32cda5d3c13b93/report.json`。
- 演示明确使用 B 确认记录替身，不是真实数据库、Worker 或前端验收。

复现真实模型演示（发送合成资料，会消耗模型额度）：

```powershell
.\backend\.venv\Scripts\python.exe scripts/run_m2_plan_demo.py --allow-live
```

## 仍待开发

1. 完整 LangGraph 暂停/恢复图与原生 checkpoint 接入。
2. 将已实现的追问模块接入 B 的分析 Worker、恢复路由与结果保存事务。
3. 与 B 的实际 Worker、确认事务及版本/删除检查集成，验证已确认计划不被改写。
4. 把累计运行和工具预算接入追问/重检索流程。
5. 待 B 的 PR #28 可集成后进行实际 Worker/数据库联调，再验证真实模型与前端。

Issue #26 保持开放；已验证的是计划草稿和单机计划确认快照，尚未完成整个 M2。代码、测试及本记录由 Codex 辅助编写，成员需理解、核验和维护。

## 本轮继续：资料追问模块

`app.agent.clarification.ClarificationFlow` 提供异步 `start` / `resume`。内部继续使用真实 M1 LangGraph 作分析，阶段间保存本地 SQLite 快照；返回等待状态时函数已结束，不占着 Worker 等用户回答。重新分析仅在用户提供了经后端核验的文档时执行。它不是 LangGraph 原生 interrupt/checkpointer 的完整实现，生成节点中途崩溃仍保守失败，不承诺任意节点恢复。

- `validate_sources(user_id, pinned_versions, new_document_ids)` 必须由 B 注入：复核归属、删除和就绪状态、原钉版，以及新文档版本；返回可信的 `list[DocumentVersion]`。不能用前端自行提交的版本充当校验结果。
- 返回 `ClarificationOutcome(status, run, pending_input)`：等待时 B 负责保存 pending_input 和执行状态；成功时 `run` 是待 B 持久化的分析结果，不能仅据此把业务执行标为成功。
- 新文档可能改变 `run.result.document_versions`。B 需要用当前输入记录和状态版本守卫，原子更新 pending 分析的钉版并保存结果；原有 M1 `save_analysis_result` 不接受不同钉版，不能直接套用后假装已保存。
- 当前 `agent.resume.resume_execution` 的生产入口仍仅支持计划确认，资料追问暂明确拒绝；尚未接入上述 B 保存事务。主分支分析 Worker 也仍按 M1 调用，不能声称网页已出现追问功能。
- 自由文本不转成 Evidence，不提供给模型冒充资料；只有明确选择继续才可保留无证据结论完成。用户上传新资料后按可信版本重新检索、分析。
- 同一输入幂等重放；不同回答冲突；其他用户、旧编号、已撤销权限、变化的原资料或重放时补充资料变化，均拒绝。
- 跨阶段保存累计预算。新增 `AGENT_MAX_CUMULATIVE_MODEL_REQUESTS=12` 与 `AGENT_MAX_CLARIFICATION_ROUNDS=3`。原检索上限与总秒数也作为整个追问执行上限保存，不因重启或重新构造对象重置。
- 每次模型请求按最大重试次数保守预留；等待用户的时间不计入调用活动秒数。进程崩溃时保留已预留额度，不自动重跑。追问次数用完仍允许明确选择按现有资料继续。

### 验证与真实演示

- 完整后端检查：338 passed、8 skipped，71.90 秒。此后额外修复版本变更重放检查，追问专项 17 项通过。未将跳过项算作通过。
- 专项包括全新 Python 子进程读取追问状态后继续；自由文本不进入证据；上传资料后真实 LangGraph 重新检索；累计预算、旧编号、权限撤销和资料变更拒绝。
- 真实模型演示结果：waiting_for_input → waiting_for_input → succeeded；自由文本未改变无证据结论，补充本地合成文件后引用其真实行号；重复恢复结果一致。
- 本次保存的累计预算：模型请求预留 8 次，检索 2 次，活动秒数约 9.73 秒。模型和本地快照真实；检索与 B 权限核验是明确替身，未使用真实 Qdrant、MongoDB 或网页。
- 实际报告：`storage/agent/m2-clarification-491de5e16dba4640a18778eb81fecfcf/report.json`。
- 演示首轮在报告读取阶段发生 SQLite 重入锁错误，已修正为复用现有连接并复跑成功；该问题在演示脚本，非业务恢复函数。

复现（发送合成资料，会消耗模型额度）：

```powershell
.\backend\.venv\Scripts\python.exe scripts/run_m2_clarification_demo.py --allow-live
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_agent_clarification.py -q
```
