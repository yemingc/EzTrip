# Plan Agent grounding baseline

本评测冻结 6 条中国城市旅行开发案例：北京历史、北京胡同餐饮、上海城市风光餐饮、成都亲子、单路线超时和未配置城市。前 4 条材料就绪并调用 Plan Agent，后 2 条分别以 `partial` 和 `blocked` 在模型调用前停止。

数据集哈希为 `1089d11ea279b19f4675b0bb75d8455cf24f7ef36c666daccd0bce09bc887dfe`。Explore、Stay、Weather 和路线均使用显式 fixture，以便隔离 Plan Agent；live 模式只把排程模型替换为 DeepSeek，并上传 LangSmith trace。

## 2026-08-21 点时结果

Fixture 回归：

- 6/6 cases；
- 4 个完整 `TripPlan`，2 个零模型调用 skip；
- 12/12 shortlist candidates covered、grounded、source-traceable、route-backed；
- 4/4 planned cases 保留完整 Weather 输出；
- 4/4 planned cases 没有把预算目标制造成 `CostItem`。

`deepseek-v4-pro` + LangSmith 隔离评测：

- 6/6 cases，4 次模型调用；
- 12/12 candidates covered、grounded、source-traceable、route-backed；
- weather preservation rate 1.0000；
- 8215 prompt tokens、852 completion tokens、9067 total tokens；
- 模型调用延迟 p50 3457 ms、p95 3800 ms。

报告位于 `evals/reports/plan-agent-fixture.v1.json` 和 `evals/reports/deepseek-plan-agent-baseline-2026-08-21.json`。Schema 位于 `evals/schemas/plan-agent-*.v1.json`。

## 指标能说明什么

这些结果证明：模型不能越过候选集合；代码能保持候选、来源、路线、天气和请求 lineage；完整日期能组装为共享 `TripPlan`；缺材料会产生可观察的零调用停止；预算目标不会被伪装为价格。

这些指标不是行程“准确率”、实时高德质量、酒店可订性、预算满足率或生产 SLA。候选目录属于开发 fixture，本隔离套件不评估营业时间、must/avoid、路线时间窗等定稿规则；这些规则由 EZ-303 Hard Validator 的独立 fixture suite 覆盖，后续 Repair Router 和产品 executor 另有独立回归。因此简历中应描述为“版本化 grounding/lineage regression”，不能写成“真实用户行程成功率 100%”。

## 复现

```powershell
Set-Location backend
uv run python -m scripts.export_plan_agent_schemas
uv run python -m scripts.run_plan_agent_eval
uv run pytest tests/test_plan_agent.py tests/test_plan_agent_evaluation.py --no-cov
```

只有在明确希望产生模型用量并上传 trace 时运行：

```powershell
uv run python -m scripts.run_plan_agent_eval --live
```
