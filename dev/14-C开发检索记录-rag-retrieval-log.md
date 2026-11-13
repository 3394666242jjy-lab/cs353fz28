# 14 C 开发检索记录（RAG 模块，#8 / #25）

状态：v0.1，2026-11 记录。本文件回答 issue #8 交付物第 4 条——"使用合成资料的开发
检索记录；真实运行与确定性替身明确区分"，并给出 #8 各验收项的当前证据位置。

与 [11 M1 联调记录](11-M1联调记录-acceptance.md) 的分工：11 号是**端到端验收记录**
（浏览器 → API → Worker → 分析 → 引用，含截图）；本文件是**模块级检索记录**，只覆盖
RAG 层——解析、切分、向量化、入库、检索本身，可以脱离 MongoDB/Celery 单独复跑。

## 1 真实运行与确定性替身的边界

这一节是本文档的核心。任何数字都必须能回答"它是跑出来的还是造出来的"。

| 类别 | 具体内容 | 是否真实调用外部服务 |
| --- | --- | --- |
| 真实运行 | 本文件第 3、4 节的检索与评测数字 | ✅ 真实百炼 Embedding、真实 Qdrant 集群、真实百炼 Rerank |
| 确定性替身 | `backend/tests` 全部单元测试 | ❌ 用 `httpx.MockTransport` 构造响应，不访问网络 |
| 仅生成、未调用 | 语料与问题集（`scripts/gen_rag_eval_corpus.py`） | ❌ 纯本地按模板生成，不含检索结果 |
| 未在本轮覆盖 | 上传→Worker→Mongo 持久化链路 | 见 dev/11（2026-11-02/03 真实环境记录） |

单元测试用替身是刻意的：它们验证的是**协议与错误路径**（维度校验、过滤条件下发、失败
不伪造证据），这些必须确定性可复现；用真实服务反而会让测试随网络和额度波动。真实调用
只用于第 3、4 节这种"取值"场景。

## 2 运行条件

| 项 | 值 | 来源 |
| --- | --- | --- |
| 解析器版本 | `pypdf-text-pages-v2`（PDF）/ `text-utf8-lf-v1`（文本） | `chunking.py` |
| 切分配置 | `block-pack-v1::500` | `CHUNK_MAX_CHARS=500` |
| Embedding | 阿里云百炼 `text-embedding-v4`，1024 维 | 真实调用，HTTP 200 |
| Rerank | 阿里云百炼 `gte-rerank-v2` | 真实调用，HTTP 200 |
| 向量库 | Qdrant 集群，集合 `careerpilot_eval`，具名 `dense` + `sparse` | 真实调用 |
| 语料 | 3 用户 × 5 文档（含 1 份 v1/v2 与 1 份三页 PDF），共 39 个 chunk | 合成，可公开评审 |

`qwen-rerank` 在本账号返回 403 AccessDenied（模型未开通），`gte-rerank` 同样 403；
实际可用的是 `gte-rerank-v2`，已实测 200。详见 dev/13 第 5 节的选型说明。

## 3 M1 交付项复核（逐条给证据）

| #8 验收项 | 结论 | 证据 |
| --- | --- | --- |
| Python PDF 逐页解析，返回原始顺序的逐页文本、1 起始页码、解析器版本 | ✅ | 实测三页 PDF：`页数 3，每页行数 [1, 0, 1]`；`PDF_PARSER_VERSION = pypdf-text-pages-v2`；locator 为 `{"kind":"pdf","page":N}` |
| PDF 按页切分、chunk 不跨页 | ✅ | 切分按页循环后串联（`indexer.index_document`），第 1 页与第 3 页各自成 chunk |
| 空白页不产 chunk，但后续页码不重排 | ✅ | 第 2 页提取为空 → 0 个 chunk；第 3 页内容仍标记 `page=3`（实测输出见下） |
| Markdown/TXT 使用真实行号 | ✅ | `TextLocator(line_start, line_end)`；`test_chunks_are_verbatim_contiguous_line_ranges` |
| quote 与原文一致、可核对空白 | ✅ | `test_fixture_quotes_stay_whole_within_one_chunk`、`test_text_chunks_keep_original_indentation` |
| 损坏 / 加密 / 无可提取文本 / 扫描件抛出可映射解析错误 | ✅ | `parse_document` 分别抛 `DocumentParseError`；`test_pdf_without_text_and_encrypted_pdf_are_rejected`、`test_scan_page_and_mixed_images_fail_closed_even_when_small`、`test_unknown_drawing_operator_fails_closed` |
| 向 B 交付逐页结果与索引元数据；向 D 保留真实定位 | ✅ | `IndexedDocument`（含 `parser_version` / `chunking_config_version` / `index_version` / 逐 chunk locator）；B 侧 `_MongoEvidenceRetriever` 组装 `quote` + `locator` |
| 可复现的合成多页 PDF，覆盖空白页后定位与失败输入 | ✅ | `scripts/gen_rag_eval_corpus.py::cert_pdf`（三页、第 2 页空白，确定性可重生成）；失败输入由单元测试覆盖 |
| 相同文档版本重复入库不产生重复片段与向量 | ✅ | `replace_document_points` 对已有向量直接拒绝（`test_existing_version_is_never_deleted_or_overwritten`）；chunk_id 与 point id 均确定性派生 |
| 用户 A 的查询不返回用户 B 的片段、非当前版本不参与默认检索 | ✅ | 检索内置 `user_id` 与版本过滤；本轮真实评测中跨用户 0/3、旧版本 0/3 违规（第 4 节） |

空白页与页码的实测输出：

```
解析器版本: pypdf-text-pages-v2
页数: 3 每页行数: [1, 0, 1]
  第 1 页 -> chunk 开头: 'AWS Certified Solutions Architect - Cert'
  第 3 页 -> chunk 开头: 'Credential ID AWS-2024-8841. This creden'
```

## 4 检索记录（真实调用）

### 4.1 M2 三档策略对比

同一索引、同一问题集、`w=0.30`、`top_k=5`；评测集 39 条问题（可回答 24 条）：

| 指标 | dense | hybrid | hybrid_rerank |
| --- | --- | --- | --- |
| Recall@1 | 0.7917 | 0.7500 | **0.8750** |
| Recall@3 | 1.0000 | 1.0000 | 1.0000 |
| MRR | 0.8819 | 0.8542 | **0.9167** |
| 引用正确性@1 | 0.7917 | 0.7500 | **0.8750** |

负例（三类可判定项）：

| 类别 | dense | hybrid | hybrid_rerank |
| --- | --- | --- | --- |
| 跨用户（3 条） | 0 违规 | 0 违规 | 0 违规 |
| 旧版本（3 条） | 0 违规 | 0 违规 | 0 违规 |
| 无证据（6 条） | 6 返回了结果 | 6 | 6 |
| 已删除（3 条） | 未判定 | 未判定 | 未判定 |

原始产物在 `backend/tests/data/rag_eval/results/`（`.eval.jsonl` 逐题、`.eval.md` 报告、
`idf.json` 的 BM25 语料统计、`tuning.json` 的权重扫描）。完整口径见 dev/13。

### 4.2 结论与边界

- 跨用户与旧版本过滤在真实调用下 0 违规——这两条是 #8 验收项，现已由检索层数据支撑；
- 无证据问题全部返回了结果：**检索层没有相关性阈值**，"资料未体现"的判定在 Agent 层，
  因此 6/6 不是检索失效，但也不能当作"没有误召回"；
- 已删除类未判定：构造"向量已被物理清理"需要 B 的 `delete_document_points`（PR #28
  未合并），暂无实测数据；
- 24 条可回答问题意味着 1 题 = 0.042 的 Recall@1，重排相对 dense 的优势是 2 题——
  只能说"在本数据集上更好"，不能说"必然更好"；
- 耗时口径为云端 Qdrant 实测值，含公网往返，**不代表本地部署开销**；真实部署是同机
  本地服务，开销需在本地环境重测。

## 5 复跑方式

```bash
# 1 重新生成语料与问题集（确定性，无需网络）
python scripts/gen_rag_eval_corpus.py

# 2 重建评测集合并索引（真实 Embedding + Qdrant）
python scripts/m2_rag_eval.py --rebuild --evaluate-only --no-proxy

# 3 三档评测（评测集，最终报告口径）
python scripts/m2_rag_eval.py --split eval --strategies dense hybrid hybrid_rerank --no-proxy

# 4 开发集权重扫描（调参只能用 dev）
python scripts/m2_rag_tune.py --weights 0.3 0.5 0.7 --no-proxy

# 5 标注对照表（人工复核用）
python scripts/m2_rag_eval.py --dump-labels
```

单元测试（确定性替身，不需要任何外部服务）：`cd backend && uv run pytest -q`。

## 6 未覆盖与限制

- 不打分 OCR：扫描件与含图形内容的 PDF 一律拒绝，这是 #8 明确的 M1 边界；
- 本轮未跑"上传 → Worker → MongoDB"链路，该链路的真实验收见 dev/11；
- 语料规模小（39 chunk / 60 题），指标存在明显抽样波动，已在上文标注噪声边界；
- 评测集只报告一次：参数在开发集上定，见 dev/13 第 4 节。
