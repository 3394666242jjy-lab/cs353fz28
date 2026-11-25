# 证据充分性评测集（成员 C）

本目录存放"**结论有没有被原文撑住**"的保留集，与 `../rag_eval`（检索召回评测）是两套
不同的东西，不能互相替代：

| | `rag_eval` | `rag_support`（本目录） |
| --- | --- | --- |
| 问题 | 该找的片段找到没有 | 找到的片段撑不撑得住这条要求 |
| 标注 | `relevant_chunk_ids` | 期望判定 `supported/partial/not_evidenced` + 证据锚点 |
| 主指标 | Recall@k / MRR | **错误支持率**、过度保守率、引用正确率 |
| 饱和现状 | Recall@3 已 100%，无区分度 | 尚无任何数字 |

## 为什么要有这个集合

`dev/13` 的三档评测只度量召回。评测集上 6 条无证据问题全部召回了候选——这不是检索
失效（检索层没有相关性阈值，判定在 Agent 层），而是说明**召回数字不能证明结论成立**。
一个看起来相关的片段只支持半条要求时，模型会不会判成完整支持，只有专门标注过强度
的题目才测得出来。

## 文件

```
rag_support/
  items.jsonl     标注：一行一条，见下
  README.md       本文件
  results/        运行产物（按对照臂落盘）
```

## 字段

```json
{"id": "s_alpha_02",
 "axis": "partially_supported",
 "user_id": "user_eval_alpha",
 "requirement": "熟悉 MySQL，并能独立完成慢查询治理与索引优化",
 "document_versions": [{"document_id": "doc_alpha_resume", "version": 1}, ...],
 "expected_status": "partial",
 "expected_evidence": [{"document_id": "doc_alpha_resume", "version": 1,
                        "anchor": "慢查询治理按业务域分摊"}],
 "forbidden_evidence": [],
 "note": "判 supported 即错误支持：索引优化没有任何原文"}
```

| 字段 | 说明 |
| --- | --- |
| `axis` | 期望的证据强度轴：`fully_supported` / `partially_supported` / `no_evidence` / `old_version` / `deleted` |
| `expected_status` | 期望判定，沿用生产三态；`not_evidenced` 表示"原文撑不住这条要求"，**不代表用户没有能力** |
| `expected_evidence` | 期望被引用的证据锚点；`supported`/`partial` 必填 |
| `forbidden_evidence` | 允许出现在上下文、但**绝不能作为引用**的片段（被取代的旧版本、已删除文档） |
| `note` | 判定依据，供人工复核；争议先改 note 再改标注 |

### 为什么用锚点而不是 chunk_id

切分参数一变 chunk_id 就漂，写死 UUID 的标注会静默指向错误片段。锚点必须在该版本
文本里**恰好命中一个 chunk**，命中 0 个或多个都直接失败（`--check` 会逐条校验）。
锚点写在文末会让整个标注集失去意义，所以锚点一律取自原文的连续子串。

## 保留集纪律

- 本集合是**保留集**：不允许拿它调切分参数或融合权重，也不允许按它的结果改提示词后
  反复重跑并只报告最好的一次；
- 参数调整只在 `rag_eval` 的开发集（`split=dev`）上做；
- 改动任何一条标注都视为新一轮评测，结果文件不得覆盖旧轮次。

## 样本量

12 条题，1 条 = 0.0833。这个规模只能回答"这类错误存不存在、大致什么量级"，
**不能**回答"哪个方案更好"（详见 dev/18 的噪声边界）。
