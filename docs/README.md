# 文档与数据导航

按[项目说明](PROJECT.md)、[结果与评测](EVALUATION.md)、[运行指南](QUICKSTART.md)顺序阅读。

| 数据或程序 | 内容 |
|---|---|
| [M1记录](../output/m1_observations.json) | 12条回答、题目、机检与人工记录口径 |
| [M2记录](../output/m2_observations.json) | 各组题项、已知费用、未知状态、延迟、usage与回答引用 |
| [40条语义登记](../output/semantic_record.json) | 每条来源、冻结机检、事后语义结论、算术合取 |
| [test_v3](../eval/test_questions_v3.json) / [criteria_m2_v2](../eval/criteria_m2_v2.json) | v3-02实际使用的冻结题集与判据 |
| [test_v4](../eval/test_questions_v4.json) / [criteria_m2_v3](../eval/criteria_m2_v3.json) | 未采用的评分候选 |
| [M1价格](../prices/prices_v1.json) / [M2价格](../prices/prices_public_20260928.json) | 对应执行日的历史公开单价快照，非当前报价 |
| [离线校验结果](../output/offline_checks.json) | 本次公开副本实跑结果与代码身份 |

`fixtures/m2-v3-02/raw/`保留70条实际回答字节及失败项的空输出占位；`fixtures/`还保留评分回归需要的旧版本。筛选后的JSON与原始报告通过来源哈希关联；它们不等于完整原始运行目录。
