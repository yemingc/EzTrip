# Plan Agent grounding baseline

本评测冻结 6 条中国城市旅行开发案例：北京历史、北京胡同餐饮、上海城市风光餐饮、成都亲子、单路线超时和未配置城市。前 4 条材料为 `ready`；单路线超时案例为可恢复的 `partial`，仍调用 Plan Agent 生成显式保留缺口的草案；未配置城市案例为 `blocked`，在模型调用前停止。

当前 fixture 数据集哈希为 `ebf2a63fd81c6ad8eedd9b649519cee0735cf107d89ad7f8e02af133bbbbba22`。Explore、Stay、Weather 和路线均使用显式 fixture，以便隔离 Plan Agent；live 模式只把排程模型替换为 DeepSeek，并上传 LangSmith trace。

## 2026-08-24 当前 fixture 结果

Fixture 回归：

- 6/6 cases；
- 5 个 `TripPlan` 草案，1 个 `blocked` 案例以零模型调用停止；
- 13/13 shortlist candidates covered、grounded、source-traceable；
- 当前案例中 13/13 已排程候选保留可用路线 lineage；单路线失败仍被材料层记录为 `partial`，不能被模型补造；
- 5/5 planned cases 保留完整 Weather 输出；
- 5/5 planned cases 没有把预算目标制造成 `CostItem`；
- fixture 模型共 5 次调用、1200 tokens；这些 token 数只用于回归，不代表 DeepSeek 成本。

## 2026-08-21 历史 live 快照

`deepseek-v4-pro` + LangSmith 隔离评测：

- 6/6 cases，4 次模型调用；
- 12/12 candidates covered、grounded、source-traceable、route-backed；
- weather preservation rate 1.0000；
- 8215 prompt tokens、852 completion tokens、9067 total tokens；
- 模型调用延迟 p50 3457 ms、p95 3800 ms。

这份 live 报告使用的是恢复契约更新前的 prompt 与用例预期，只能作为当时的模型调用和 trace 证据，不能与上面的当前 fixture 数字直接合并。报告位于 `evals/reports/plan-agent-fixture.v1.json` 和 `evals/reports/deepseek-plan-agent-baseline-2026-08-21.json`。Schema 位于 `evals/schemas/plan-agent-*.v1.json`。

## 指标能说明什么

这些结果证明：模型不能越过候选集合；代码能保持候选、来源、可用路线、天气和请求 lineage；完整日期能组装为共享 `TripPlan`；可恢复缺口会形成显式降级草案，真正不可规划的材料才会零调用停止；预算目标不会被伪装为价格。

这些指标不是行程“准确率”、实时高德质量、酒店可订性、预算满足率或生产 SLA。候选目录属于开发 fixture，本隔离套件不评估营业时间、must/avoid、路线时间窗等定稿规则；这些规则由 EZ-303 Hard Validator、Repair Router 和 Product Graph 的独立回归覆盖。因此简历中应描述为“版本化 grounding/lineage regression”，不能写成“真实用户行程成功率 100%”。

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
