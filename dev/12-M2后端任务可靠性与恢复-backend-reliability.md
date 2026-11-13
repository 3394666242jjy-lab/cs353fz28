# M2 后端任务可靠性与恢复说明

状态：v0.1 随 Issue #24 交付。覆盖 B 侧 M2 的开发环境、数据迁移、新增配置与失败恢复语义；不承诺 exactly-once，所有链路按至少一次投递加幂等消费设计（dev/03 生命周期 #5）。

## 开发环境

- `compose.yaml` 的 MongoDB 以**单节点副本集 `rs0`** 运行：M2 的计划确认与执行恢复把多文档写入放进 MongoDB 事务，事务只存在于副本集/分片集群。健康检查在首次启动时自动执行 `rs.initiate`，首次 `docker compose up -d` 后约 10–20 秒变为 healthy，属正常现象。
- `.env` 的 `MONGODB_URI` 建议带 `?directConnection=true`（见 `.env.example`）：驱动跳过副本集拓扑发现，直连本地节点即可使用事务，避免容器内主机名不可解析的问题。
- 旧版本地数据卷（单节点数据）可以直接沿用：加 `--replSet` 启动并完成 initiate 后原有数据保留，无需导出导入。
- 验证事务可用：`uv run --project backend pytest backend/tests/test_m2_integration.py -q`（需要 `RUN_MONGO_M2_TESTS=1` 与运行中的 compose 环境）。

## 新增集合与迁移

以下集合均为**新增**，索引由服务层 `_ensure_indexes` 在首次访问时幂等创建，不需要离线迁移脚本：

| 集合 | 用途 | 关键索引 |
| --- | --- | --- |
| execution_events | SSE 业务事件持久化（stage/waiting_for_input/completed/failed） | id 唯一；(execution_id, sequence) 唯一 |
| dispatch_events | start/resume 指令的 outbox，至少一次发布 | id 唯一；(status, created_at) |
| execution_inputs | 追问/确认输入，恢复幂等锚点 | id 唯一；(execution_id, interrupt_id) 唯一 |
| plans / plan_tasks | 计划草稿与正式任务 | plans.id 唯一；plan_tasks (plan_id, id) 唯一 |

对 M1 数据的兼容变更（均为新增字段，旧代码读取不受影响）：

- `executions` 新增 `event_seq`（事件序号计数器）与 `resource_version`（索引任务的目标版本）；缺失时分别视为 0 与 1。
- `document_versions` 新增 `source_key`（该版本的解析来源）；M1 旧版本行缺失时回退到 `documents.storage_key`。
- `documents.purge_status` 在 M1 已存在（active）；删除后置为 pending。

回滚：以上变更全部为增量，回滚代码后旧版本可继续读写；如需清理可删除上述五个新集合，不影响 M1 集合。

## 新增配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| EVENT_HISTORY_RETENTION_COUNT | 200 | 每个执行最多保留的事件条数；更早的游标在 SSE 返回 410 EVENT_HISTORY_EXPIRED |
| SSE_POLL_INTERVAL_SECONDS | 1.5 | SSE 从 MongoDB 轮询新事件的间隔 |
| SSE_HEARTBEAT_SECONDS | 15 | SSE 注释心跳间隔（不落库） |

## 失败恢复语义

- **Outbox（dispatch_events）**：业务写入与指令同事务提交；发布在提交后进行且**先发布后标记**，进程在两者之间崩溃会导致重复投递而不是丢失。Worker 用 `expected_state_version` 的守卫式状态迁移认领，重复消息被状态机丢弃，不产生重复副作用。API 进程的恢复循环（30s）重投所有 pending 指令。
- **执行恢复**：沿用 M1 的租约/超时回收；M2 起索引任务按 `resource_version` 判定成败——重建版本（v2+）崩溃只把该版本行置 failed，已发布版本继续服务；v1 崩溃维持 M1 行为（文档置 failed）。恢复触发的终态同样写入 execution_events，SSE 订阅者可观察到。
- **删除与清理**：DELETE 先写 deleted_at 阻断所有读与检索（列表、详情、证据、分析 stale 均即时生效），再异步清理。purge 任务先标记 chunks 删除、再按 user_id+document_id 过滤物理删除 Qdrant 点、最后删除文件；每一步幂等。向量删除失败按指数退避重试 5 次（60s 起），仍失败置 purge_status=failed，由恢复循环每分钟窗口重投。
- **SSE 断线**：客户端凭 Last-Event-ID（或 after_event_id）重连，服务端按序列回放后持续推送；轮询式实现不存在"先回放后订阅"的丢失窗口。游标超出保留窗口返回 410，客户端读取 `GET /executions/{id}` 快照（含 latest_event_id）后重连；任务已终结且无历史时，服务端从快照合成一条终态事件后关闭流。
- **计划确认与恢复**：确认在同一事务中写入 plan_tasks（稳定 ID）、execution_inputs（内容哈希幂等）与 resume dispatch，并把执行从 waiting_for_input 切回 queued；重复确认返回已保存结果，不重复创建任务或指令。图恢复由 D 的模块执行（Issue #26），未集成时任务显式失败并说明原因，不伪装成功。
- **不承诺 exactly-once**：跨 MongoDB、Redis、Qdrant 与文件系统没有分布式事务；所有重复通过稳定 ID、守卫式状态迁移与内容哈希幂等吸收。
