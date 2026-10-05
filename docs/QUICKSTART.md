# 运行指南

安装Python 3并克隆仓库。在项目根目录运行，Windows可将`python`替换为`py -3`。只需要标准库。

```bash
python verify_m1.py
python verify_m2.py
python tools/build_scoring_candidate_v3.py --out output/rebuilt_test_v4.json --criteria-out output/rebuilt_criteria_m2_v3.json --report output/build_report.json
python tools/verify_scoring_candidate_v3.py --report output/verify_report.json
```

M1/M2校验启动并停止本机回环桩，不调用模型。M1需要本地端口5299–5301空闲；如被占用，释放端口再运行。测试产物在`runs/`；M1会清理自身前缀的旧测试目录，建议在独立克隆中运行。

候选构建输出到`output/`，不覆盖`eval/`原件。验证器默认寻找与`--report`同目录的`build_report.json`；缺文件退出2。旧报告或候选哈希不一致退出1；可显式传`--build-report`指向正确报告。候选为离线诊断，未采用。

公开副本将候选验证器的历史默认路径改为相对`fixtures/`，评分规则与合成逻辑保持源程序。夹具中的清单/对账JSON为筛选字段；70条回答文件与三个旧评分参照保持原字节。公开副本实跑结果见[offline_checks.json](../output/offline_checks.json)。

查看[结果与评测](EVALUATION.md)并通过[数据导航](README.md)打开已有回答，不需要凭据。公开包没有真实执行批准文件，以上命令均可离线使用。真实调用需要项目使用者另行准备有效批准、身份匹配与凭据，不能沿用历史批次剩余额度。
