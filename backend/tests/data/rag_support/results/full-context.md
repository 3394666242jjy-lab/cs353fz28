### 对照臂 `full-context`

- 指标口径：`support-eval-v1`；判定模型：`unavailable`
- 题目 12 条，已判定 0 条，未判定 12 条
- 无可判定样本，本轮不产出任何比率

| 指标 | 值 |
| --- | --- |
| 状态完全一致率 (`status_accuracy`) | 无样本 |
| 错误支持率 ↓ (`false_support_rate`) | 无样本 |
| 过度保守率 ↓ (`over_refusal_rate`) | 无样本 |
| 引用失效证据率 ↓ (`stale_citation_rate`) | 无样本 |
| 引用正确率 ↑ (`citation_precision`) | 无样本 |
| 引用召回 ↑ (`citation_recall`) | 无样本 |

上下文体积：平均 4073 字 / 最多 4149 字 / 平均 10.8 块；超预算被跳过的题 0 条
成本：输入 0 tokens，输出 0 tokens；耗时 P50 0 ms / P95 0 ms

| 期望 \ 判定 | supported | partial | not_evidenced |
| --- | --- | --- | --- |
| supported | 0 | 0 | 0 |
| partial | 0 | 0 | 0 |
| not_evidenced | 0 | 0 | 0 |

> 本臂 12 条未判定（判定模型未运行），比率只覆盖已判定样本，不能当作该策略的能力结论

> 全资料基线把钉住版本的全部片段直接给模型：小语料下它可能必赢，结论必须连同上下文体积一起读，不能外推到资料量更大的场景
