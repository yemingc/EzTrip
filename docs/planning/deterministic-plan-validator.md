# Deterministic Plan Validator V1

EZ-103 把预算算术和基础计划冲突放在普通代码中处理。入口是：

```python
validate_trip_plan(request: TripRequest, plan: TripPlan) -> PlanValidationReport
```

模型不参与校验，也不能决定预算是否满足。相同 typed input 会得到相同预算汇总、issue ID、规则结果和修复责任。

## V1 规则

| 范围 | 规则 | 失败结果 |
|---|---|---|
| 请求关联 | `request_id` 一致 | `plan.request_mismatch` |
| 目的地 | 请求与计划城市一致 | `plan.destination_mismatch` |
| 日期 | 请求与计划起止日期一致 | `plan.date_window_mismatch` |
| 候选 | candidate ID 跨天不重复 | `plan.duplicate_candidate` |
| Grounding | 推荐活动来源只能是 live/fixture provider | `source.invalid_grounding_mode` |
| 预算下界 | 已知费用下界不超过 limit | `budget.deterministic_floor_exceeds_limit` |
| 预算上界 | 费用区间上界不造成未处理超支 | `budget.possible_overrun` |
| 费用覆盖 | included category 均有显式 CostItem | `budget.incomplete_category_coverage` |
| 最终状态 | 有错误时不得提前标记 final | `plan.finalized_with_errors` |

`TripPlan` 自身的 Pydantic 契约已经在 Validator 之前拒绝缺失旅行日、item ID 重复、日内时间重叠、未知费用引用和未知天气风险引用。Validator 负责需要同时观察 request 与 plan，或需要返回 typed issue 而非 schema exception 的规则。

## 预算语义

预算只统计 `BudgetConstraint.included_categories` 内的 `CostItem`，其他费用仍保留在 `excluded_cost_item_ids`，不会消失。所有乘法和求和使用 `Decimal`：

```text
total_minimum = sum(quantity × unit_price.minimum)
total_maximum = sum(quantity × unit_price.maximum)
```

结果分为：

- `within_limit`：费用类别完整且区间上界不超过预算；
- `possible_overrun`：下界可行但上界可能超支；
- `exceeded`：确定性下界已经超支；
- `incomplete`：预算声明包含某类别，但尚无对应 CostItem；
- `not_requested`：用户没有给预算，系统不作预算满足保证。

硬预算的 `possible_overrun`、`exceeded` 或 `incomplete` 都阻止 finalization；软预算产生 warning。显式零费用需要一个来源可追溯、单价为 0 的 CostItem，不能通过缺失数据暗示零元。

## 可重放示例

`trip-request.v1.json` 的 3000 元预算包含交通、餐饮、门票和活动，当前 `trip-plan.v1.json` 只有一个 120 元门票 CostItem。Validator 因此返回 `incomplete`，而不是错误地报告“只花 120 元、预算满足”。

```powershell
Set-Location backend
uv run python -m scripts.export_plan_validation_example
uv run pytest tests/test_plan_validator.py tests/test_domain_contract_examples.py --no-cov
```

## 与 Hard Validator 的边界

EZ-103 的基础 Validator 保持稳定，继续服务旧纵向切片和 Plan Agent 草案装配。EZ-303 的 [`hard-validators.md`](./hard-validators.md) 在它之上消费 planning materials 与独立营业时间证据，新增 must/avoid、路线可行性、候选城市/血缘和营业窗口规则。

当前仍没有 Repair Router、天气风险局部修复或酒店间夜价格/库存校验。`RECALCULATE_BUDGET`、`REPLAN_DAY`、`RERUN_EXPLORE`、`RERUN_ROUTE` 和 `ASK_USER` 已是可评测的 typed responsibility/repair contract，但不表示修复工作流已经执行。
