### 对照臂 `full-context`

- 指标口径：`support-eval-v1`；判定模型：`qwen-plus`
- 题目 12 条，已判定 12 条，未判定 0 条
- 可判定样本 12 条：1 条 = 0.0833，比率只有量级意义

| 指标 | 值 |
| --- | --- |
| 状态完全一致率 (`status_accuracy`) | 0.9167 |
| 错误支持率 ↓ (`false_support_rate`) | 0.0000 |
| 过度保守率 ↓ (`over_refusal_rate`) | 0.0000 |
| 引用失效证据率 ↓ (`stale_citation_rate`) | 0.0000 |
| 引用正确率 ↑ (`citation_precision`) | 0.2143 |
| 引用召回 ↑ (`citation_recall`) | 0.5000 |

上下文体积：平均 4073 字 / 最多 4149 字 / 平均 10.8 块；超预算被跳过的题 0 条
成本：输入 39856 tokens，输出 1185 tokens；耗时 P50 2803 ms / P95 4808 ms

| 期望 \ 判定 | supported | partial | not_evidenced |
| --- | --- | --- | --- |
| supported | 3 | 0 | 0 |
| partial | 0 | 3 | 0 |
| not_evidenced | 0 | 1 | 5 |

> 全资料基线把钉住版本的全部片段直接给模型：小语料下它可能必赢，结论必须连同上下文体积一起读，不能外推到资料量更大的场景
