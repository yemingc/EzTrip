# Hard Validators V1

EZ-303 在 Plan Agent 的 grounded `TripPlan` 草案之后增加确定性定稿门禁：

```python
validate_hard_trip_plan(request, plan, materials, opening_hours)
```

入口不调用 LLM。它复用 `validate_trip_plan` 的请求/计划身份、重复候选、来源模式和预算重算，再消费 `PlanningMaterialBundle` 与独立 `OpeningHoursEvidenceBundle` 检查跨对象硬规则。输出仍是统一的 `PlanValidationReport`，但 `validator_version` 为 `hard-trip-plan-validator-v1`，便于下一阶段 Repair Router 直接消费。

## 规则与责任路由

| 规则 | 失败条件 | responsible_node | repair_action |
|---|---|---|---|
| hard must_visit | 已确认 hard 地点未在适用日期出现 | `explore` | `rerun_explore` |
| hard avoid | 已确认 hard 避开地点被安排 | `explore` | `rerun_explore` |
| candidate scope/lineage | shortlist 未被恰好覆盖，或名称/来源被改写 | `plan` / `validator` | `replan_day` / `none` |
| single city | POI 或住宿锚点城市与请求不一致 | `explore` / `stay` | `rerun_explore` / `rerun_stay` |
| route presence/endpoints/lineage | 缺路线、端点不邻接或不来自当前矩阵 | `route` | `rerun_route` |
| transfer window | 路线分钟数大于相邻活动间隔 | `plan` | `replan_day` |
| activity density | 显式节奏下每日主活动少于或多于约定范围 | `explore` / `plan` | `rerun_explore` / `replan_day` |
| meal structure | 餐饮被排成主要活动，或推荐未绑定当日主活动 | `plan` | `replan_day` |
| meal proximity | 推荐餐厅与绑定活动的直线距离超过 3 公里 | `explore` | `rerun_explore` |
| first-leg departure | 住宿到首站的路线无法反推或匹配建议出发时间 | `plan` | `replan_day` |
| excessive transfer | 显式节奏下单段路线超过 90 分钟 | `route` / `plan` | `rerun_route` / `replan_day` |
| opening-hours evidence | 对应日期没有 Provider 证据 | `explore` | `rerun_explore` |
| opening-hours window | 活动不落在任何已验证窗口内 | `plan` | `replan_day` |
| hard budget | CostItem 下界超限、区间可能超限或类别缺失 | `budget` | `ask_user` / `recalculate_budget` |

`TripPlan` 自身已经拒绝日期缺口、item ID 重复和日内重叠；基础 Validator 已检查 candidate 重复、请求/目的地/日期一致性和来源模式。Hard Validator 不重复把这些 schema 规则包装成 Agent。

餐饮 proximity 当前依据 Provider 经纬度计算直线距离，不等同于真实步行路线；前端必须同时展示该边界。价格、营业时间、排队和可订状态没有独立事实时也不得从推荐对象推断。

## 营业时间事实边界

`OpeningHoursEvidenceBundle` 与候选对象分离，要求：

- 每个窗口带 `candidate_id`、服务日期、带时区开闭时间和 `SourceReference`；
- 来源只能是 `live` 或显式 `fixture`，必须有 `provider_id`；
- bundle 的 request ID 与 data mode 必须匹配当前规划材料；
- 同一地点可有多个分段营业窗口；活动完整落入其中任意一个窗口才通过。

当前高德候选没有稳定、结构化的营业时间字段。因此生产数据接入前，Validator 会诚实返回 `opening_hours.evidence_missing`，而不会把空字段当作全天开放。提交的 fixture 只验证契约和时间算术，不证明实时营业状态。

## must/avoid 边界

V1 只执行 `confirmed=true` 且 `strength=hard` 的约束。地点名经过 Unicode NFKC、大小写、空白和标点规范化后做精确匹配；不使用模糊 LLM 判断。“故宫”与“故宫博物院”一类别名仍需要上游约束绑定或用户确认，避免 substring 误把宽泛类别当作具体地点。

## 可重放评测

```powershell
Set-Location backend
uv run python -m scripts.export_hard_validator_schemas
uv run python -m scripts.run_hard_validator_eval
uv run pytest tests/test_hard_validator.py tests/test_hard_validator_evaluation.py --no-cov
```

`evals/cases/hard-validator/suite.v1.json` 冻结 12 条场景：正常已确认必去、缺失必去、命中 avoid、缺路线、转场不足、营业证据缺失/越界、候选/路线血缘错配、POI/住宿跨城和硬预算价格缺口。

提交报告记录：

- 12/12 cases；
- 12/12 exact issue sets；
- 22/22 issue severity、responsible node 与 repair action；
- 12/12 deterministic replays；
- Hard Validator 0 次模型调用。

这些指标是开发 fixture 上的规则回归，不是实时 Provider 准确率、预算满足率、行程质量分数或 Repair 成功率。后续 [`repair-router.md`](repair-router.md) 消费这些 typed issues，独立验证有界、定向重跑契约。
