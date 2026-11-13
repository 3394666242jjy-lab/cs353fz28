### 策略 `dense`

- 检索配置版本：`dense/text-embedding-v4@1024/top5`
- 可回答问题：24

| 指标 | 值 |
| --- | --- |
| recall_at_1 | 0.7917 |
| recall_at_3 | 1.0000 |
| recall_at_5 | 1.0000 |
| mrr | 0.8819 |
| citation_precision_at_1 | 0.7917 |
| citation_precision_at_3 | 0.3333 |
| citation_precision_at_5 | 0.2000 |

耗时（ms）：P50 332.8，P95 632.1，平均 embedding 335.0 / 检索 25.9

| 负例类别 | 数量 | 违规 | 违规率 |
| --- | --- | --- | --- |
| no_evidence | 6 | 6 | 100.00% |
| cross_user | 3 | 0 | 0.00% |
| old_version | 3 | 0 | 0.00% |
| deleted | 3 | 0 | 0.00% |
