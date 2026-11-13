# M2 集成收尾与验收

日期：2026-11-13。关联 #32、#23、#25、#26；基于已合并的 #28、#29、#30、#31。仅使用合成资料、隔离账号和独立数据库/向量集合。原成员贡献保留，Codex 辅助接线、修复、测试和记录。

## 本次补齐

- JD 新增 `PATCH /api/v1/jobs/{id}`，必须携带 `expected_version` 与完整 `raw_jd`。归属不符返回 404，版本冲突 409，空白文本 422。CAS 更新只改变当前岗位；既有分析保留岗位快照，读取时显示 stale，旧计划不可确认。
- Worker 的 hybrid/hybrid_rerank 同时写入关键词稀疏向量；查询时注入编码器和按用户、文档、精确版本查 MongoDB 的原文解析器。候选在送外部 Rerank 前复核当前资料，删除及旧版本不得成为重排原文或 Agent 证据。
- 生产关键词使用固定 `keyword-fixed-v1`（IDF=1，平均长度=100），不使用评测集统计。索引版本包含编码器参数指纹，检索版本标注固定配置。实验继续使用已留档的拟合 IDF，二者的质量结果不能混为同一配置。
- 无候选的重排正常返回空证据；资料编辑后前端追踪真实索引终态，失败保留旧已发布版本。退出/切换账号重建资料组件。
- 修复 React StrictMode 下计划草稿恢复永远停在“正在恢复”的问题；清理失效 effect 后允许重新读取草稿。
- SSE 支持跨 chunk 的 CRLF 分帧；空连接有重试上限，401 清理会话，非事件流响应明确失败并交由页面回退轮询。
- 评测删除负例先实际删除目标文档全部向量，再查询，最后重建该隔离文档供后续策略使用。新记录有 `deletion_verified`，旧记录仍保持未判定；耗时不含删除与恢复准备阶段，稀疏检索耗时计入 search。

## 真实验收

最终检查：完整后端 **524 passed，无跳过**（81.67 秒，含真实数据库及检索生命周期）；Ruff 检查/格式通过；前端 typecheck、build、format:check 通过；SSE 客户端 3 项检查通过；真实 API 验收 23 项全部通过；三档检索共 117 次问题查询完成。

环境：独立 WSL MongoDB 8.0 副本集、Redis 7.0、Qdrant 1.19.2，Windows FastAPI/Celery solo 与 Vite。复用本机已配置的百炼 `text-embedding-v4@1024`、`gte-rerank-v2`、`qwen-plus`，未修改用户 `.env`。所有密钥、测试身份、数据库与日志留在忽略目录。

| 场景 | 结果与证据 |
| --- | --- |
| PDF 上传、索引、引用原文 | 真实模型与 API 通过，定位保留 PDF 页码 |
| 证据不足暂停、自由文本、新资料恢复 | 通过；自由文本不冒充证据，重复回答幂等 |
| 暂停点 Worker 重启 | 重启后恢复通过；累计预算与 checkpoint 仍受约束 |
| 计划草稿、确认与重复确认 | 确认前无正式任务，重复确认任务 ID 相同 |
| 版本更新/删除后的恢复与确认 | 409 拒绝，其他账号 404 |
| 删除立即阻断 | 故意停止 purge 投递，证实向量仍存在时三档业务检索均为空、资料与原文接口均 404 |
| 物理清理重试 | 实际执行 purge 两次，向量与版本文件清理完成，状态 done |
| 三档与 B/D 联调 | 实际 Worker 的 hybrid_rerank 完成模型分析；独立 live 检查三档新版本、旧版本、跨用户及删除边界 |
| 前端资料编辑 | 浏览器实际保存 v2，页面自动从处理中变为已就绪 |
| JD 编辑 | 浏览器实际保存 v1、PATCH 为 v2，再使用 v2 真实分析 |
| 前端刷新恢复 | 真实分析结果、未确认计划草稿可恢复，修复后确认可写入正式任务 |
| SSE | API 鉴权、回放、Last-Event-ID 续传、过期 410、快照恢复与终态停连；客户端另测 CRLF、去重、断线续传、重试上限和 401 |

脱敏实际记录与截图见 `dev/acceptance/2026-11-13-m2-*`。此前 dev/12、dev/13、dev/14 中“未合并、删除未判定、待联调”等说明是原始交付时点记录；当前状态以本文和新结果为准。

## 检索比较与默认选择

同一合成问题集 eval 切分 39 条，其中 24 条可回答；本地 Qdrant、真实百炼模型，top5、w=0.30。

| 指标 | dense | hybrid | hybrid_rerank |
| --- | --- | --- | --- |
| Recall@1 | 0.7917 | 0.7500 | 0.8750 |
| Recall@3 | 1.0000 | 1.0000 | 1.0000 |
| MRR | 0.8819 | 0.8542 | 0.9167 |
| 删除负例违规 | 0/3 | 0/3 | 0/3 |
| 跨用户负例违规 | 0/3 | 0/3 | 0/3 |
| 旧版本负例违规 | 0/3 | 0/3 | 0/3 |

每档的耗时及逐题结果在 `backend/tests/data/rag_eval/results/*.eval.*`，已按本地运行刷新。无证据问题 6/6 仍召回候选，说明检索层没有证据充分性阈值；Agent 必须结合原文判定 not_evidenced，不得把召回当成能力证明。24 条可回答问题中 1 条即 0.042 的 Recall@1，不能宣称总体准确率或最优权重。

默认保持 dense：当前样本不足以证明增加外部调用和成本适用于所有资料。三档均可配置使用。启用 hybrid 或 hybrid_rerank 前，必须在新的双字段集合中重新索引资料；旧 dense-only 集合可继续稠密使用，禁止直接删除共享集合或覆盖已发布资料版本。迁移时应保留既有引用，按新资料版本发布索引，确认实际模型和配置后再切换流量。

## 复现与边界

```powershell
cd backend
uv run pytest
uv run ruff check --config pyproject.toml app tests ../scripts
uv run ruff format --config pyproject.toml --check app tests ../scripts
cd ../frontend
npm run typecheck
npm run build
npm run format:check
node ../scripts/test_frontend_events.mjs
cd ..
uv run --project backend python scripts/m2_acceptance.py --include-lifecycle-checks
uv run --project backend python scripts/m2_rag_eval.py --rebuild --split eval --strategies dense hybrid hybrid_rerank --no-proxy
```

完整真实 Mongo 检查设置 `RUN_MONGO_AUTH_TESTS=1`、`RUN_MONGO_JOB_TESTS=1`、`RUN_MONGO_STORAGE_TESTS=1`、`RUN_MONGO_M2_TESTS=1`。真实检索生命周期检查另外设置 `RUN_RAG_M2_TESTS=1`，会对合成文本调用已配置的收费 Embedding/Rerank 服务。它随机创建并清理专用测试数据库与集合，不能指向生产服务。

单机暂停点恢复、私有本地文件存储属于本次交付边界；多机 checkpoint、OSS/HTTPS 云部署、M3 任务看板、模拟面试产品功能和 OCR 均未纳入本次完成声明。模型调用正中崩溃保守失败，不保证任意节点续跑或 exactly-once。main 合并仍需遵循仓库协作规则，不能以通过自测替代其他成员审查。
