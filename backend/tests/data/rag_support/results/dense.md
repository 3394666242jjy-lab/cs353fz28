### 对照臂 `dense`

- 指标口径：`support-eval-v2`；判定模型：`沿用已有判定（本次未调用模型）`
- 题目 12 条，已判定 12 条，未判定 0 条
- 检索配置：`dense/text-embedding-v4@1024/top5`
- 可判定样本 12 条：1 条 = 0.0833，比率只有量级意义

| 指标 | 值 |
| --- | --- |
| 状态完全一致率 (`status_accuracy`) | 1.0000 |
| 错误支持率 ↓ (`false_support_rate`) | 0.0000 |
| 过度保守率 ↓ (`over_refusal_rate`) | 0.0000 |
| 引用失效证据率 ↓ (`stale_citation_rate`) | 0.0000 |
| 引用正确率 ↑ (`citation_precision`) | 0.4000 |
| 引用召回 ↑ (`citation_recall`) | 1.0000 |
| 数字锚点覆盖率 ↑ (`numeric_anchor_rate`) | 无样本 |

> 数字锚点覆盖率无可核对锚点：本题集的要求里没有数字

上下文体积：平均 2071 字 / 最多 2427 字 / 平均 5.0 块；超预算被跳过的题 0 条
成本：输入 23597 tokens，输出 1276 tokens；总耗时 P50 5569 ms / P95 6509 ms
分阶段耗时（均值，ms）：embedding 522 / 向量库 1854 / 重排等残差 0 / 判定 2942；检索段 P50 2336 / P95 2462

| 期望 \ 判定 | supported | partial | not_evidenced |
| --- | --- | --- | --- |
| supported | 3 | 0 | 0 |
| partial | 0 | 3 | 0 |
| not_evidenced | 0 | 0 | 6 |
