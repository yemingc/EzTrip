# Single Planner 基线（2026-08-21）

本报告在 EZ-008 冻结的 10 条中国旅行 planning-seed 上评估 `single-planner-v1`。上游候选仍来自显式标注的 fixture/scenario provider；只有 Planner 模型使用真实 `deepseek-v4-pro`，并通过 LangSmith 记录 trace。

对应机器可读报告：`evals/reports/deepseek-single-planner-baseline-2026-08-21.json`。

## Point-in-time 结果

| 指标 | 结果 |
|---|---:|
| 全部 workflow cases | 10 |
| 候选就绪并调用模型 | 6 |
| 按上游状态停止、未调用模型 | 4 |
| 通过路由与结构检查 | 10/10 |
| 输入候选 / 已安排候选 | 6 / 6 |
| candidate coverage | 1.0000 |
| candidate grounding | 1.0000 |
| source traceability | 1.0000 |
| Prompt / completion tokens | 5603 / 632 |
| Total tokens | 6235 |
| 模型调用 p50 / p95 | 2797 / 3968 ms |

4 条停止案例分别覆盖：无已确认必去候选、未配置城市、未确认必去约束和 provider timeout。它们不会为了凑出计划而调用模型或伪造候选。

## 这些数字能说明什么

- forced-tool 输出能被契约解析；
- 只有 `candidates_ready` 请求会进入 Planner；
- 已安排 candidate ID 与 provider 输入集合完全一致；
- `DayPlan` 中的标题和来源逐项来自候选，而非模型生成；
- 日期和时间线满足当前结构契约。

## 这些数字不能说明什么

- 10/10 不是行程质量、景点推荐准确率或生产成功率；
- 每个可规划案例只有一个必去候选，尚未测试多候选排序、日内组合和路线效率；
- provider 是 fixture，本报告不评估高德实时覆盖或 SLA；
- 没有营业时间、天气、路线、酒店、费用或预算可行性输入；
- 输出只覆盖已有候选的部分 DayPlan，不是完整 `TripPlan`；
- 还没有与多 Agent 系统进行对照，因此不能声称多 Agent 带来提升。

## 重跑

```powershell
Set-Location backend
uv run python -m scripts.run_single_planner_eval --live
```

该命令需要本地 DeepSeek 与 LangSmith 配置，会产生 6 次模型调用。报告中的 dataset SHA256 必须与 planning-seed 保持一致；代码与 schema 测试会验证聚合值不能与逐案例结果冲突。
