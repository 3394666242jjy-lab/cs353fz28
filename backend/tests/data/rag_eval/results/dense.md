### 策略 `dense`

- 检索配置版本：`dense/text-embedding-v4@1024/top5`
- 可回答问题：36

| 指标 | 值 |
| --- | --- |
| recall_at_1 | 0.6944 |
| recall_at_3 | 0.9167 |
| recall_at_5 | 1.0000 |
| mrr | 0.8171 |
| citation_precision_at_1 | 0.6944 |
| citation_precision_at_3 | 0.3056 |
| citation_precision_at_5 | 0.2000 |

耗时（ms）：P50 4545.0，P95 22335.4，平均 embedding 722.0 / 检索 6830.9
（耗时为云端 Qdrant（careerpilot_eval 所在集群）实测值，含公网往返，不代表本地部署开销）

| 负例类别 | 数量 | 违规 | 违规率 |
| --- | --- | --- | --- |
| no_evidence | 9 | 9 | 100.00% |
| cross_user | 6 | 0 | 0.00% |
| old_version | 6 | 0 | 0.00% |
| deleted | 3 | 3 | 未判定（待清理链路） |
