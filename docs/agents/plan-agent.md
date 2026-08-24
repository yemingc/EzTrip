# Plan Agent

EZ-302 把 Explore、Stay、主动 Weather、路线材料和预算目标首次合成为完整的多 Agent `TripPlan` 草案。EZ-406A.1 又把主活动与餐饮建议拆成两条契约：模型只排 Provider-grounded 的主要游览活动，普通代码负责地理分组、路线可行性、附近餐饮绑定、事实、来源、金额边界与结构正确性。

## 输入门禁

Plan Agent 只接受 `PlanningMaterialBundle.status=ready`：

- Explore shortlist 至少包含一个有 Provider 来源的 POI；
- Stay 已给出一个住宿区域锚点；
- 每日住宿锚点到首站、以及相邻主活动之间的计划链路路线完整；
- 预算已经被确定性 allocator 转成目标 envelope；
- Weather 分支已经主动查询并返回风险或明确的空结果。

材料为 `partial` 或 `blocked` 时，返回 `PlanAgentRunStatus.skipped` 和 `materials_not_ready`，模型调用数固定为 0。V1 不让模型猜缺失路线、房间数或预算。

## 模型与代码的职责

DeepSeek 通过强制 `submit_grounded_schedule` tool call 只能提交：

- `candidate_id`；
- `day_number`；
- `start_time`；
- 供审计的 `reason`。

模型看到已确认约束、同行人、按日地理分组的主活动候选、路线时长、逐日天气风险和预算目标，可以做软权衡，但不能设置标题、来源、路线对象、价格、酒店库存、营业时间或稳定 ID。餐厅候选不会进入该 tool contract，因而不能被模型排成主要活动。

确定性 normalizer 负责：

- 要求 shortlist 中每个主活动 POI 恰好出现一次，并拒绝未知、重复或餐饮 ID；
- 从候选对象回填名称、类别、来源与建议时长；
- 保留确定性的逐日地理分组，并根据真实路线时长调整相邻活动时间；
- 从路线材料回填每天住宿锚点到首个 POI、以及相邻 POI 之间的 `RouteLeg`，同时反推建议离店时间；
- 从独立餐饮候选中为每天选择最多两个距离当日活动不超过 3 公里的 `MealRecommendation`；推荐不含强制开始/结束时间，也不进入活动密度计数；
- 把 Weather 专业分支的风险原样写入 `TripPlan`，并按日期生成 `weather_risk_ids`；
- 为没有 POI 的日期生成明确的 `free_time` 草案，使计划覆盖旅行的每一天；
- 生成稳定 `plan_id` 和 item ID，再调用现有 deterministic Validator。

住宿候选当前只作为路线锚点，不创建可预订的住宿行程项。高德住宿 POI 没有实时房价或库存，重复写入多晚还会造成错误的可订暗示。后续接入可信住宿价格/库存来源后再扩展该契约。

## 预算事实边界

`budget-allocator-v1` 输出的是类别目标金额，不是价格。Plan Agent 因此不会从目标金额制造 `CostItem`。当请求含预算但上游没有价格事实时，Validator 稳定返回 `budget.incomplete_category_coverage`；软预算是 warning，硬预算则会阻止 finalization。

这意味着系统可以说“预算材料已用于排程权衡”，不能说“当前计划已经验证不超预算”。

## 下游校验与当前边界

Plan Agent 只形成 grounded draft，不自动定稿。独立的 `validate_hard_trip_plan` 消费同一 request、planning materials、TripPlan 和营业时间证据，除 must/avoid、路线时间窗、城市、来源血缘、营业窗口及硬预算外，还检查节奏对应的每日活动密度、餐饮未进入时间线、餐饮邻近锚点、首站出发时间和超长通勤。

当前仍未包含：

- 真实步行路网意义上的餐厅距离；当前 3 公里阈值是 Provider 坐标直线距离；
- 餐厅价格、实时营业、排队或可订状态；
- 远郊唯一目的地的用户确认型例外；超过 90 分钟的单段路线当前默认阻断；
- 天气变化后的后台定时重规划；天气只在生成或修改请求时查询；
- 酒店实时价格、库存、预订、票务或支付；
- 一次请求内的跨城市城际排程。

这些边界使系统可以证明“主活动密度、相邻路线和餐饮推荐进入了可校验的同构 `TripPlan`”，又不会把直线距离、fixture 或待验证事实包装成真实预订能力。
