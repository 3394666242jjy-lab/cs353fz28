# 18 证据充分性评测（成员 C）

状态：v0.1，2026-11。关联 [issue #25](https://github.com/3394666242jjy-lab/cs353fz28/issues/25)
的技术债与下一步计划（[dev/17](17-M3切分与知识库基线-m3-rag.md) 第 5 节），并为 D 的
双通道路由（#39）准备可复核的口径。本文件记录**基座**：指标定义、标注集、对照臂与
本轮真实跑到的边界，不宣称任何支持判定质量的结论。

## 1 为什么要有这一层

M2 的三档评测（[dev/13](13-M2检索策略与评测-rag-m2-spec.md)）只度量"片段找回没有"。
现在的实测事实是：

| 现象 | 说明什么 |
| --- | --- |
| 评测集 Recall@3 三档全为 1.0000 | 召回指标已饱和，再拿它当主指标没有区分度 |
| 6 条无证据问题全部召回了候选 | 检索层没有相关性阈值，**判定不在检索层** |
| `partial` 与 `supported` 都要求引用证据 | 三态契约已有，但没有任何东西度量它判得对不对 |

所以值得补测的不是"再调一档检索策略"，而是：**当一个片段看起来相关、实际只支持一部
分时，模型会不会把它判成完整支持。** 这个错误对用户的伤害比召回低一位更直接——他会
拿到一个没有依据的结论。

## 2 口径

### 2.1 三态判定（沿用生产契约，不自造第四态）

`supported` / `partial` / `not_evidenced`，语义与
`backend/app/agent/prompts.py::ASSESS_PROMPT`、`app.agent.models.Assessment` 完全一致：

- `supported` 必须引用证据且无未解决缺失项；
- `partial` 必须引用证据**并**列出缺失部分；
- `not_evidenced` 不得引用证据，且**不代表候选人没有能力**。

`backend/app/rag/support_eval.py::SupportJudgement` 复刻这三条约束并有单测锁定，避免
评测口径与线上契约分叉。

### 2.2 标注轴

| 轴 | 期望 | 构造方式 |
| --- | --- | --- |
| `fully_supported` | `supported` | 要求被某段原文整体覆盖 |
| `partially_supported` | `partial` | 要求含两个子项，原文只覆盖其中一个（如"MySQL + 索引优化"里只有慢查询治理） |
| `no_evidence` | `not_evidenced` | 语料完全没有相关内容（含负责人举的"LangGraph 编排与中断恢复"场景） |
| `old_version` | `not_evidenced` | 钉住新版本后，旧结论已被取代；引用旧片段即失效引用 |
| `deleted` | `not_evidenced` | 文档已删除，向量已物理清理 |

其中 `partially_supported` 与 `no_evidence` 里刻意各放了一个"相关片段存在但撑不住"的
陷阱（`s_alpha_02`、`s_gamma_03`）：面试记录写了"测试习惯良好"，但与"覆盖率的量化
成果"无关。**这类题判成 `supported` 就是本层要抓的错误。**

### 2.3 指标

| 指标 | 定义 | 方向 |
| --- | --- | --- |
| `false_support_rate` 错误支持率 | 期望不是 `supported` 的题里，被判成 `supported` 的比例 | ↓ 主指标 |
| `over_refusal_rate` 过度保守率 | 期望是 `supported` 的题里，被判成 `not_evidenced` 的比例 | ↓ 与上者成对 |
| `stale_citation_rate` 引用失效证据率 | 有禁用片段的题里，引用了旧版本/已删除片段的比例 | ↓ |
| `citation_precision` 引用正确率 | 判成 supported/partial 时，引用落在期望证据上的比例 | ↑ |
| `citation_recall` 引用召回 | 期望证据被引用的比例 | ↑ |
| `status_accuracy` 状态一致率 | 三态完全一致的比例 | ↑ |

三条纪律：

1. **未判定不参与任何比率**。判定模型没跑就是 `null`，不折算成 0 或 1；报告里单列
   "未判定 N 条"。
2. **没有样本返回 `null` 而不是 0**。0 会被读成"没有这个错误"。
3. 每条报告都带样本分辨率（12 条题 → 1 条 = 0.0833），避免把费率读成精度。

## 3 标注集与保留集纪律

`backend/tests/data/rag_support/items.jsonl`，12 条，轴分布：完整支持 3、部分支持 3、
无证据 3、旧版本 2、已删除 1。字段与规则见同目录 README。

两个刻意的设计：

- **证据用原文锚点标注，不写 chunk_id**。切分参数一变 chunk_id 就漂；锚点必须在该
  版本里**恰好命中一个 chunk**，命中 0 个或多个都直接失败。`--check` 逐条校验。
- **这是保留集**：不允许拿它调切分参数或融合权重，也不允许改提示词后反复重跑只报
  最好的一次。参数调整仍只在 `rag_eval` 的开发集上做。

## 4 对照臂

同一个判定模型、同一份标注、同一上下文预算（默认 6000 字）下比较，差异只能来自
"上下文从哪来"：

| 臂 | 上下文 | 需要什么 |
| --- | --- | --- |
| `full-context` | 该题钉住版本下的**全部**片段 | 无（纯本地，离线可跑） |
| `dense` / `hybrid` / `hybrid_rerank` | M2 检索路径的 top-k 片段 | 真实 Embedding + Qdrant |

`full-context` 是"把全部资料直接塞给同一个模型"的那一档，用来回答"RAG 到底带来了
什么"。**它必须连同上下文体积一起读**：小语料下全文必然塞得下，赢了也不代表生产上
该这么做。

删除轴逐题处理（与 M2 评测一致）：只在该题测前物理删掉目标文档的向量、测后恢复，
其他题看到的仍是完整语料，不会被这一道题拖成"少一份资料"。

## 5 本轮实际跑到哪一步

### 5.1 已执行（离线，无外部服务）

```
backend\.venv\Scripts\python.exe scripts/rag_support_eval.py --check
  → 标注校验通过：12 条

backend\.venv\Scripts\python.exe scripts/rag_support_eval.py --arm full-context --judge none
  → 12 条全部装配成功
```

`full-context` 臂的上下文体积（`results/full-context.json`）：

| 项 | 值 |
| --- | --- |
| 平均上下文 | 4072.6 字 / 10.8 块 |
| 最大上下文 | 4149 字 |
| 超预算被跳过的题 | 0 |

删除轴那条题（`s_alpha_05`）装配出 9 块 / 3382 字，比同用户其他题少 2 块，与"该文档
已被物理删除"一致。

### 5.2 未执行，以及为什么

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 支持判定质量（全部六个指标） | ❌ 未判定 | 需要真实判定模型（`--judge provider`）；本轮未配置，全部标为未判定，**不产出任何比率** |
| 三档检索臂 | ⏸ 未跑 | 需要真实 Embedding 与 Qdrant；本轮只验证了 `full-context` 装配 |
| 与 D 的生产 `status` 对齐 | ⏸ 待协作 | 生产三态由 D 的 pipeline 产出；本基座先冻结口径与标注，联调时直接用同一份 items 复跑 |

因此**本文件不含任何"支持判定准确/不准确"的结论**。当前能说的只有：口径已冻结、
标注集可用、上下文体积已量化。

## 6 噪声边界与不能宣称的结论

- 12 条题，1 条 = 0.0833。只能回答"这类错误存不存在、大致什么量级"，不能回答
  "哪个方案更好"；
- 语料是合成的、每题证据多为 1 个 chunk，真实简历的"部分支持"更模糊；
- `old_version` 与 `deleted` 两轴的 `stale_citation_rate` 在**有版本过滤**的臂上结构
  上为 0，它的价值是**防回归**：一旦有人放松版本过滤或删除阻断，这个比率会立刻跳出 0；
- `full-context` 臂在小语料下天然占优，任何"全资料更好/更差"的结论都必须带上上下文
  体积与语料规模。

## 7 复跑方式

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_support_eval.py tests/test_support_eval_runner.py -q

cd ..
backend\.venv\Scripts\python.exe scripts/rag_support_eval.py --check
backend\.venv\Scripts\python.exe scripts/rag_support_eval.py --arm full-context --judge none

# 真实判定（需要配置生成模型）
backend\.venv\Scripts\python.exe scripts/rag_support_eval.py --arm full-context --judge provider

# 三档检索臂对照（需要真实 Embedding + Qdrant）
backend\.venv\Scripts\python.exe scripts/rag_support_eval.py --compare full-context dense hybrid_rerank --judge provider --rebuild
```

## 8 失败处理

- 判定模型未配置：`--judge provider` 直接失败，不回退到任何离线替身；
- `--judge none`：只统计上下文体积，报告里所有比率显示"无样本"，并明确标注未判定条数；
- 锚点漂移（切分或语料变了）：解析直接失败，不沉默地指向别处；
- 钉住的版本不在语料里：显式 `SystemExit`，不拿残缺上下文继续算比率。

## 9 与 D / 负责人的协作点

1. **判定语义**：三态仍由 D 的 pipeline 产出，本基座只提供标注与指标，不接管 Agent；
2. **联调方式**：D 侧只需把 `status` + 引用 chunk_id 按 items 对齐输出，即可用同一套
   指标复算，不需要改本基座；
3. **决策用途**：错误支持率与过度保守率是一对——只顾压低前者会把所有要求判成
   `not_evidenced`，那同样是把有依据的经历否掉。任何调优都必须两个数一起报。
