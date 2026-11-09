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
{"id": "q_alpha_01",
 "user_id": "user_eval_alpha",
 "query": "候选人用的服务框架是哪个版本？",
 "kind": "answerable",
 "style": "literal",
 "relevant_chunk_ids": ["385c27cd-..."],
 "document_versions": [{"document_id": "doc_alpha_resume", "version": 1}],
 "note": "标注依据：FastAPI 0.115.0 出现在 resume v1"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 全局唯一；`q_<user>_<n>` 为可回答，`neg_<user>_<n>` 为负例 |
| `user_id` | 是 | 检索发起人；跨用户负例填发起人，相关 chunk 属于他人 |
| `query` | 是 | 问题原文 |
| `kind` | 是 | `answerable` / `no_evidence` / `cross_user` / `old_version` / `deleted` |
| `style` | 是 | `literal`（含字面 token，hybrid 应占优）/ `paraphrase`（只能语义命中）/ `negative` |
| `expect` | 负例必填 | `empty`（结果必须为空）或 `forbidden_absent`（允许有结果，但旧版本 chunk 不得出现） |
| `relevant_chunk_ids` | answerable 必填 | 人工标注的相关 chunk，长度为 1 |
| `forbidden_chunk_ids` | `forbidden_absent` 必填 | 被取代版本里绝不能出现的 chunk |
| `document_versions` | 是 | 该问题应传入检索的钉住版本，模拟 B 的快照行为 |
| `note` | 是 | 标注依据，便于复核与争议追溯 |

`old_version` 负例必须用 `forbidden_absent` 而不能用 `empty`：语义相近的**新版本**
chunk 本就应该被召回，用"结果为空"去要求它属于误判。

## 当前数据集构成

由 `scripts/gen_rag_eval_corpus.py` 确定性生成（改语料或标注请改脚本后重新生成，
不要手改产物）：

| 项 | 规模 |
| --- | --- |
| 用户 | 3 个，互不相关技术栈（Python / Java / 前端） |
| 文档 | 每用户 5 份，其中 1 份含 v1、v2 |
| chunk | 每用户 13 个（`CHUNK_MAX_CHARS=500` 下） |
| 问题 | 60 个：36 可回答 + 24 负例 |
| 负例分布 | 无证据 9、跨用户 6、旧版本 6、已删除 3 |
| PDF | 每用户 1 份三页，第 2 页空白（验证空白页之后的页码定位） |

标注的 `chunk_id` 由生成器直接调用 `parse_document` + `chunk_document` 再经
`make_chunk_id` 派生，与运行期 `index_document` 完全一致，不会漂移。

## 使用约束

- 问题集与语料版本化，随结果一起留档；改动问题集即视为新评测轮次；
- `no_evidence` 与 `cross_user` 等负例的命中率单独统计，不与 Recall 混算；
- 标注由人工复核后冻结，不得为了指标好看事后调整标注。
