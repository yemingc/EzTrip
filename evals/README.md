# EzTrip evaluations

本目录保存可版本化、可由 CI 校验的场景、schema、fixture 和报告。

Gate 0 目前只有 3 条 specification-level smoke case：

| Case | 覆盖目标 |
|---|---|
| `smoke-normal-beijing-3d-v1` | 正常请求能够保留约束，并产生可重算、可追溯的三日计划 |
| `smoke-budget-conflict-beijing-3d-v1` | 确定性成本下界超过预算时返回冲突，不静默放宽约束 |
| `smoke-weather-risk-beijing-3d-v1` | 天气工具主动发现第二天风险，系统定位受影响项目并提出局部重排 |

这些 Gate 0 case 不是完整 `TripRequest`、provider fixture 或 golden output。EZ-008 已在 `cases/planning-seed/` 冻结 6 条 standard + 4 条 hard 可执行请求；EZ-101 在 `cases/constraint-agent/` 为同一批中文输入冻结独立约束标签；EZ-102 复用 planning-seed 检查单 Planner 是否只消费可追溯候选、正确停止并生成部分 DayPlan。两个 Agent 的真实 DeepSeek point-in-time 报告都保存在 `reports/`。

验证命令：

```powershell
Set-Location backend
uv run pytest tests/test_smoke_cases.py --no-cov
uv run pytest tests/test_planning_seed_eval.py tests/test_constraint_agent_evaluation.py tests/test_single_planner_evaluation.py --no-cov
```
