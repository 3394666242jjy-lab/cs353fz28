# M2 检索评测数据集（成员 C，#25）

本目录存放 M2 三档检索策略对比用的**合成**语料与问题集。规范见 [dev/13](../../../dev/13-M2检索策略与评测-rag-m2-spec.md)。

## 目录约定

```
rag_eval/
  corpus/            合成语料，多用户、多文档、多版本
  questions.jsonl    问题与证据标注，一行一个问题
  README.md          本文件
```

**禁止**放入任何真实简历、真实姓名、真实联系方式。语料一律由模板合成，可公开评审。

## 语料要求

| 维度 | 要求 |
| --- | --- |
| 用户数 | ≥ 2（必须能构造跨用户负例） |
| 每用户文档数 | ≥ 3（Markdown/TXT 为主，含 ≥ 1 个多页 PDF 用于页码定位） |
| 版本 | 至少一个文档有 v1、v2（用于旧版本过滤与 stale） |
| 删除 | 至少一个文档在评测中被软删除（用于删除阻断负例） |
| 内容 | 需包含只有字面匹配才能命中的片段（数字、专有名词、版本号），否则无法体现 hybrid 与 dense 的差异 |

## questions.jsonl 字段

```json
{"id": "q001",
 "user_id": "user_eval_a",
 "query": "候选人在哪个项目里用了 Qdrant？",
 "kind": "answerable",
 "relevant_chunk_ids": ["chunk_..."],
 "document_versions": [{"document_id": "doc_...", "version": 1}],
 "note": "标注依据：第三段明确提到 Qdrant"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 全局唯一，负例用 `neg_` 前缀便于统计 |
| `user_id` | 是 | 检索发起人；跨用户负例填发起人，相关 chunk 属于他人 |
| `query` | 是 | 问题原文 |
| `kind` | 是 | `answerable` / `no_evidence` / `cross_user` / `deleted` / `old_version` |
| `relevant_chunk_ids` | answerable 必填 | 人工标注的相关 chunk；其余类别为空数组 |
| `document_versions` | 是 | 该问题应传入检索的钉住版本，模拟 B 的快照行为 |
| `note` | 是 | 标注依据，便于复核与争议追溯 |

## 使用约束

- 问题集与语料版本化，随结果一起留档；改动问题集即视为新评测轮次；
- `no_evidence` 与 `cross_user` 等负例的命中率单独统计，不与 Recall 混算；
- 标注由人工复核后冻结，不得为了指标好看事后调整标注。
