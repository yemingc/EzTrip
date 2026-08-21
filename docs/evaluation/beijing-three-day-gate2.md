# 北京三日最小纵向切片（Gate 2）

EZ-104 首次把此前隔离验证的组件接成一条可重放路径：

```text
TripRequest
  → compile_context / clarification_gate / candidate_search
  → provider-grounded Single Planner
  → deterministic TripPlan assembler
  → deterministic plan and budget Validator
```

## 为什么先用 fixture

Gate 2 的目标是验证组件边界与失败语义，不是宣称实时旅行质量。两个案例使用同一组版本化北京 POI、固定 Planner placement 和明确标注的测试费用，因此 CI 可以精确重放；它们不依赖 API Key、网络或 LLM 随机性，也不会把测试费用描述成市场价格。

固定 Planner 仍经过 `Single Planner → deterministic normalizer` 子图。模型形状只能提交 candidate ID、day、start time 和 reason；名称、来源、日期、结束时间与稳定 ID 仍由代码组装。该执行模式证明主链可以连接，不衡量 DeepSeek 的规划质量；真实 DeepSeek 指标保留在独立 Single Planner 基线中。

## 两个可执行案例

| 案例 | 预期 | 机械结果 |
| --- | --- | --- |
| 正常预算 | 三个 provider 候选分别覆盖三天，费用可重算 | 3 个 DayPlan；500 CNY fixture 总额；validation passed |
| 硬预算冲突 | 保留全部必去点与费用类别，不静默放宽 | 900 CNY fixture 下界对 300 CNY 上限；600 CNY 缺口；conflicted；不可 final |

提交报告记录：2/2 cases、20/20 deterministic checks、6/6 candidate sources traceable、2/2 exact replays。完整正常结果也单独提交，能够沿 `request_id`、`context_id`、candidate ID、provider source、plan ID 和 validation report 复核阶段血缘。

## 重放

```powershell
Set-Location backend
uv run python -m scripts.export_vertical_slice_schemas
uv run python -m scripts.run_vertical_slice_eval
uv run pytest tests/test_vertical_slice.py --no-cov
```

只有显式执行以下命令才会重写已提交产物；默认运行只读：

```powershell
uv run python -m scripts.run_vertical_slice_eval --write-report
```

版本化证据：

- 输入：`evals/cases/beijing-vertical-slice/suite.v1.json`
- schema：`evals/schemas/vertical-slice-suite.v1.json` 与 `vertical-slice-report.v1.json`
- 聚合结果：`evals/reports/beijing-three-day-gate2.v1.json`
- 完整正常结果：`evals/reports/beijing-three-day-gate2-normal-result.v1.json`

## Gate 2 结论与边界

Gate 2 的机械条件已经满足：正常请求能形成完整三日 `TripPlan`；预算只从 `Decimal CostItem` 重算；所有推荐地点来自 provider fixture；不可满足硬预算返回 typed conflict 且不自动 final；相同输入与固定模型提案可以精确重放。

这仍不是产品级行程：没有开放式候选扩展、实时营业时间、路线矩阵、天气汇合、酒店房态、真实价格、HITL 执行、checkpoint 恢复或多 Agent 对照。下一阶段可以在这条已验证的骨架上加入 state/checkpoint，再按责任拆 Explore/Stay 等专业节点。
