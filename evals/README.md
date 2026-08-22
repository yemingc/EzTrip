# EzTrip evaluations

本目录保存可版本化、可由 CI 校验的场景、schema、fixture 和报告。

Gate 0 从 3 条 specification-level smoke case 起步：

| Case | 覆盖目标 |
|---|---|
| `smoke-normal-beijing-3d-v1` | 正常请求能够保留约束，并产生可重算、可追溯的三日计划 |
| `smoke-budget-conflict-beijing-3d-v1` | 确定性成本下界超过预算时返回冲突，不静默放宽约束 |
| `smoke-weather-risk-beijing-3d-v1` | 天气工具主动发现第二天风险，系统定位受影响项目并提出局部重排 |

这些 Gate 0 case 不是完整 `TripRequest`、provider fixture 或 golden output。EZ-008 已在 `cases/planning-seed/` 冻结 6 条 standard + 4 条 hard 可执行请求；EZ-101 在 `cases/constraint-agent/` 为同一批中文输入冻结独立约束标签；EZ-102 复用 planning-seed 检查单 Planner 是否只消费可追溯候选、正确停止并生成部分 DayPlan；EZ-202 在 `cases/explore-agent/` 冻结 6 条开放式景点/餐饮开发案例与人工相关性标签；EZ-203 在 `cases/stay-agent/` 冻结 4 条住宿区域筛选与 2 条调用前阻断案例；EZ-204 在 `cases/specialist-fanout/` 冻结 5 条并行、部分失败、能力跳过与阻断案例；EZ-301/302 分别冻结路线/预算材料和多 Agent Plan Agent 套件；EZ-303 在 `cases/hard-validator/` 冻结 12 条 must/avoid、路线、营业时间证据、跨城、来源和硬预算规则案例；EZ-304 在 `cases/repair-router/` 冻结 9 条定向修复、重试上限、不可修复与 HITL 案例。真实 DeepSeek point-in-time 报告与确定性 fixture 报告保存在 `reports/`。

Hard Validator fixture 报告验证 12/12 exact issue sets、22/22 责任路由、12/12 确定性重放和 0 Validator 模型调用。这些数字不代表实时数据准确率或自动修复成功率。

Repair Router fixture 报告验证 9/9 exact action + executed-node routes、9/9 重试上限、9/9 未受影响节点复用、9/9 确定性重放和 0 Router 模型调用。fixture executor 只模拟责任节点产物，因此这些数字不代表 live Agent 自动修复成功率。

验证命令：

```powershell
Set-Location backend
uv run pytest tests/test_smoke_cases.py --no-cov
uv run pytest tests/test_planning_seed_eval.py tests/test_constraint_agent_evaluation.py tests/test_single_planner_evaluation.py tests/test_explore_agent_evaluation.py tests/test_stay_agent_evaluation.py --no-cov
uv run pytest tests/test_specialist_fanout.py tests/test_specialist_fanout_eval.py --no-cov
uv run pytest tests/test_hard_validator.py tests/test_hard_validator_evaluation.py --no-cov
uv run pytest tests/test_repair_router.py tests/test_repair_router_evaluation.py --no-cov
```
