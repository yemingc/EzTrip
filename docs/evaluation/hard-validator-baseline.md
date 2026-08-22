# Hard Validator fixture baseline

EZ-303 用版本化 fixture 验证 Hard Validator 是否能稳定发现问题，并把问题归给下一阶段可执行的最小责任节点。

## 数据集

- suite：`evals/cases/hard-validator/suite.v1.json`
- schema：`evals/schemas/hard-validator-suite.v1.json`
- report：`evals/reports/hard-validator-fixture.v1.json`
- case 数：12
- 执行模式：fixture，Validator 0 次模型调用

场景覆盖已确认 must/avoid、路线缺失与转场窗口、营业时间证据缺失/越界、候选和路线来源血缘、POI/住宿跨城、硬预算缺少价格事实，以及一条包含真实 hard must_visit 结构的正常成都案例。

## 结果

- 12/12 cases passed；
- exact issue set rate：1.0000；
- issue routing accuracy：22/22，1.0000；
- deterministic replay：12/12；
- Hard Validator model calls：0。

`routing accuracy` 要求 rule code、severity、responsible node 和 repair action 同时匹配冻结标签。它不是路线 API 准确率；修复执行由独立的 [`repair-router-baseline.md`](repair-router-baseline.md) 验证。

## 边界

- POI、住宿、路线和营业时间都是显式 fixture；
- 营业时间窗口只证明时间比较与来源约束，不代表当前场馆实际开放；
- must/avoid 使用规范化精确地点名，别名绑定尚未实现；
- 预算仍只信任 `CostItem`，目标 envelope 不会被转换成虚假价格；
- suite 是开发回归集，不是未触碰 holdout。

## 重放

```powershell
Set-Location backend
uv run python -m scripts.export_hard_validator_schemas
uv run python -m scripts.run_hard_validator_eval
uv run pytest tests/test_hard_validator.py tests/test_hard_validator_evaluation.py --no-cov
```
