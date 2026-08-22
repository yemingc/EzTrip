# Weather Repair Coordinator

## 目标

Weather Repair 处理“行程已经生成后，Provider 返回新的显著天气风险”这一类系统事件。天气事实不是用户追加输入：Weather Provider 提供结构化 `WeatherRisk`，确定性 Coordinator 负责定位受影响活动、创建局部重规划任务、校验候选方案并决定自动采用还是进入 HITL。

## 执行链

```text
latest WeatherRisk snapshot
  -> significant severity filter
  -> aware datetime overlap
  -> candidate environment/type match
  -> WeatherRepairTask
  -> injected Plan replan executor (max 2)
  -> local-scope guard
  -> residual weather-impact check
  -> Hard Validator
  -> minor auto-apply | major pending_confirmation | unresolved
```

Weather 分支和 Coordinator 都不调用模型。只有注入的重规划执行器可以进行一次 schema-constrained Plan 调用，并且每个任务最多尝试两次。

## 影响匹配

V1 仅让 `medium`、`high`、`extreme` 风险进入修复。风险必须同时满足：

- 城市与 `TripPlan.destination_city` 一致；
- 风险区间与活动的 aware datetime 区间真实重叠；
- `affected_activity_types` 与 shortlist 候选的 `environment`、category 或 tag 确定性匹配；
- 活动来自 shortlist 中可追溯的 attraction/meal 候选。

同一天但时间不重叠、户外风险只覆盖室内活动、活动类型不匹配或低等级风险都不会创建重规划任务。

## 局部作用域与变更分级

执行器只能修改受影响 item。未受影响 item、费用事实、天气事实、请求身份和旅行日期必须保持不变。跨日移动可以把受影响 item 放到另一日期，但不能顺带改写目标日期已有活动。

- `minor`：候选集合与所在日期不变，只发生同日时间或顺序调整；通过全部校验后可自动采用。
- `major`：跨日移动、候选集合变化或多个日期变化；有效方案标记为 `pending_confirmation`，原计划继续作为 `effective_plan`。
- `unresolved`：执行失败、越界改写、仍暴露在天气风险中、没有实际变化或 Hard Validator 失败；最多两次后停止。

HITL 在此阶段是明确的 typed outcome 和待确认方案。API 级暂停、恢复、审批操作及持久化将在产品任务 Graph 中接线。

## 可重放证据

```powershell
Set-Location backend
uv run python -m scripts.export_weather_repair_schemas
uv run python -m scripts.run_weather_repair_eval
uv run pytest tests/test_weather_repair.py tests/test_weather_repair_evaluation.py --no-cov
```

版本化 10-case fixture 覆盖无风险、低风险、室内误报、时间不重叠、类型不匹配、minor 自动采用、跨日 major HITL、作用域越界、执行器持续失败和风险仍未消除。提交报告只证明编排、安全和来源契约，不代表天气预报准确率、实时高德 SLA 或真实模型的重规划质量。
