# EzTrip evaluations

本目录保存可版本化、可由 CI 校验的场景、schema、fixture 和报告。

Gate 0 目前只有 3 条 specification-level smoke case：

| Case | 覆盖目标 |
|---|---|
| `smoke-normal-beijing-3d-v1` | 正常请求能够保留约束，并产生可重算、可追溯的三日计划 |
| `smoke-budget-conflict-beijing-3d-v1` | 确定性成本下界超过预算时返回冲突，不静默放宽约束 |
| `smoke-weather-risk-beijing-3d-v1` | 天气工具主动发现第二天风险，系统定位受影响项目并提出局部重排 |

这些 case 不是完整 `TripRequest`、provider fixture 或 golden output。`expectations[].code` 只是稳定的场景验收标签，不是生产 API 枚举。真实领域 schema 和高德字段稳定后，再冻结 6 条 standard + 4 条 hard 种子请求。

验证命令：

```powershell
Set-Location backend
uv run pytest tests/test_smoke_cases.py --no-cov
```
