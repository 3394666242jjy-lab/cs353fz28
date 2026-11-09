# 13 M2 检索策略与评测（成员 C，#25）

状态：v0.1，S0 阶段（基线、集合结构、评测口径已冻结）。关联 [issue #25](https://github.com/3394666242jjy-lab/cs353fz28/issues/25)、[issue #8](https://github.com/3394666242jjy-lab/cs353fz28/issues/8)。

M2 的目标不是把检索做"更强"，而是让三档策略**在可复现条件下比出结论**，并保证资料生命周期（新版本、旧版本、删除）在检索阶段严格生效。没有数据支撑的收益一律不写进结论。

## 1 依赖与边界

| 依赖 | 提供方 | 状态 |
| --- | --- | --- |
| 资料版本快照、删除阻断、向量清理 | 成员 B（PR #28 / dev/12） | 已实现，待合并 |
| 证据原文与 locator 组装 | 成员 B `_MongoEvidenceRetriever` | 已实现 |
| 证据消费与 stale 判定 | 成员 D / B 路由 | 已实现 |
| OCR、生产部署 | — | 不在本任务 |

检索层的职责边界不变：C 只返回候选身份与分数，`quote`/`locator` 的权威副本在 MongoDB，由 B 按属主查询后补齐。

## 2 集合结构变更（迁移须知）

M2 起 `careerpilot_chunks` 集合必须同时具备两个命名字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `dense` | 1024 维 Cosine | 稠密语义向量，M1 已有 |
| `sparse` | 稀疏向量，无模型配置 | 关键词表示，权重由客户端按 BM25 计算 |

payload 字段与索引不变（`corpus`、`user_id`、`document_id`、`version`、`chunk_id`、`index_version`、`deleted`）。

**迁移要求**：M1 时期创建的集合没有 `sparse` 字段。`ensure_collection()` 会校验并抛出可执行错误（提示删除集合并重建），**不会**在运行时静默降级。重建后需重新索引全部资料；Qdrant 是可重建索引，MongoDB 中的 chunk 原文与 locator 不受影响。

写入侧对 `sparse` 是可选的：只写稠密向量的点仍然合法，因此 M1 代码路径在新集合上照常工作。

## 3 三档策略与版本号

| 档位 | 召回 | 说明 |
| --- | --- | --- |
| `dense` | 稠密向量 top-k | M1 基线 |
| `hybrid` | 稠密 + BM25 稀疏，按权重融合 | 补字面匹配 |
| `hybrid_rerank` | `hybrid` 候选再经 Rerank 服务重排 | 需配置 `RERANK_*` |

`retrieval_config_version` 格式在 `backend/app/rag/retrieval.py::build_retrieval_config_version` 中冻结：

```
dense/text-embedding-v4@1024/top5
hybrid/w0.50/text-embedding-v4@1024/top5
hybrid_rerank/gte-rerank/w0.50/text-embedding-v4@1024/top5
```

任一参数变化都产生新版本号，旧结果不得被覆盖。评测记录必须连同版本号一起留档，否则无法说明三档是在同一条件下比较的。

## 4 冻结基线

| 参数 | 值 | 冻结理由 |
| --- | --- | --- |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | `text-embedding-v4` / 1024 | 换模型即换版本号 |
| `CHUNK_MAX_CHARS` | 500 | 进入 `chunking_config_version` |
| `RETRIEVAL_TOP_K` | 5 | 评测的 k 值 |
| `HYBRID_SPARSE_WEIGHT` | 0.5 | 三档对比必须锁定同一 w |
| 数据集 | 合成语料 + 合成问题集 | 不使用真实简历 |

调参只允许在两处发生：开发集上定切分参数、开发集上定融合权重；定完锁死再进共享评测。**不允许**在评测集上反复调参后报告最好结果。

## 5 稀疏表示方案

采用**客户端 BM25**（决策 A），不依赖外部服务：

- 分词与词频在客户端计算，IDF 由受控语料统计得出，随评测数据集一起留档；
- 与稠密通道按权重融合后写入/查询 Qdrant 的 `sparse` 字段；
- 备选方案是 DashScope 的稀疏端点，但它是模型给出的稀疏表示，语义上与稠密通道重叠，跑出的差异难以归因，因此只作为后续对照，不作为 M2 结论。

## 6 评测口径

### 指标

| 指标 | 定义 |
| --- | --- |
| Recall@k | 标注相关集合中被前 k 个结果覆盖的比例 |
| MRR | 首个相关结果的倒数排名 |
| 引用正确性 | 前 k 个结果中属于标注相关集合的比例 |
| 误召回率 | 无证据问题里返回非空结果的占比，**单独统计**，不与 Recall 混算 |
| 耗时 | 检索阶段 P50/P95 毫秒，分 embedding / 存储 /（rerank）三段记录 |

**耗时口径**：生产部署的向量库是同机本地服务。若某一轮评测跑在临时云端环境，延迟
会被公网往返放大，报告必须在数字旁边注明"云端实测值，不代表本地部署开销"，不得
把它当作部署开销结论。运行器会按向量库地址自动判断并写入口径说明。

### 负例类别

每个策略都必须覆盖，且必须全部通过：

| 类别 | 期望 |
| --- | --- |
| `no_evidence` | 返回空 + 明确原因，不伪造证据 |
| `cross_user` | 不返回他人资料 |
| `deleted` | 软删除后立即检索不到 |
| `old_version` | 被取代的版本不进入候选 |

### 数据集格式

见 `backend/tests/data/rag_eval/README.md`。

## 7 失败处理

- 外部服务未配置（无 `RERANK_API_KEY`、无 Embedding Key）：**该档位必须显式失败**并标注"未完成"，不得静默降级，也不得用离线替身冒充真实结果；
- 检索为空：返回空候选 + 原因，绝不生成假证据；
- 向量库/模型错误：如实向上传播，由上层标记失败。

## 8 与 B/D 的契约现状

- 版本快照由 B 在创建分析时按 `document.current_version` 钉住，经 worker 传入 `retrieve_evidence`；C 忠实按传入列表过滤，不自作判断；
- stale 由 B/D 在读时派生（`analysis_is_stale`），C 不参与；
- 删除由 B 先写 `deleted_at` 阻断读取，purge 再按 `user_id + document_id` 物理删除向量；C 侧 `deleted` payload 条件保留为纵深防御；
- 待对齐项：`delete_document_points` 的删除粒度（整份文档全部版本 vs 单个版本）需与 B 确认。
