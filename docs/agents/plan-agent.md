# Plan Agent

EZ-302 把 Explore、Stay、主动 Weather、路线矩阵和预算目标首次合成为完整的多 Agent `TripPlan` 草案。实现仍遵守同一条工程边界：模型负责需要语义权衡的排程，普通代码负责事实、来源、金额边界与结构正确性。

## 输入门禁

Plan Agent 只接受 `PlanningMaterialBundle.status=ready`：

- Explore shortlist 至少包含一个有 Provider 来源的 POI；
- Stay 已给出一个住宿区域锚点；
- POI 之间以及住宿锚点到 POI 的有向路线完整；
- 预算已经被确定性 allocator 转成目标 envelope；
- Weather 分支已经主动查询并返回风险或明确的空结果。

材料为 `partial` 或 `blocked` 时，返回 `PlanAgentRunStatus.skipped` 和 `materials_not_ready`，模型调用数固定为 0。V1 不让模型猜缺失路线、房间数或预算。

## 模型与代码的职责

DeepSeek 通过强制 `submit_grounded_schedule` tool call 只能提交：

- `candidate_id`；
- `day_number`；
- `start_time`；
- 供审计的 `reason`。

模型看到已确认约束、同行人、候选环境、路线时长、逐日天气风险和预算目标，可以做软权衡，但不能设置标题、来源、路线对象、价格、酒店库存、营业时间或稳定 ID。

确定性 normalizer 负责：

- 要求 shortlist 中每个 POI 恰好出现一次，并拒绝未知或重复 ID；
- 从候选对象回填名称、类别、来源与建议时长；
- 从路线矩阵回填每天住宿锚点到首个 POI、以及相邻 POI 之间的 `RouteLeg`；
- 把 Weather 专业分支的风险原样写入 `TripPlan`，并按日期生成 `weather_risk_ids`；
- 为没有 POI 的日期生成明确的 `free_time` 草案，使计划覆盖旅行的每一天；
- 生成稳定 `plan_id` 和 item ID，再调用现有 deterministic Validator。

住宿候选当前只作为路线锚点，不创建可预订的住宿行程项。高德住宿 POI 没有实时房价或库存，重复写入多晚还会造成错误的可订暗示。后续接入可信住宿价格/库存来源后再扩展该契约。

## 预算事实边界

`budget-allocator-v1` 输出的是类别目标金额，不是价格。Plan Agent 因此不会从目标金额制造 `CostItem`。当请求含预算但上游没有价格事实时，Validator 稳定返回 `budget.incomplete_category_coverage`；软预算是 warning，硬预算则会阻止 finalization。

这意味着系统可以说“预算材料已用于排程权衡”，不能说“当前计划已经验证不超预算”。

## 当前未包含的能力

EZ-302 只形成 grounded draft，不自动定稿，也没有提前实现后续阶段：

- 营业时间、路线时间窗、must/avoid 和其他 Hard Validators；
- 根据 `ValidationIssue` 选择最小责任 Agent 的 Repair Router；
- 天气变化后的局部重规划；
- 酒店实时价格、库存、预订、票务或支付；
- 产品 API 与前端审核界面。

这些边界使本阶段能单独证明“多 Agent 材料确实进入同构 `TripPlan`”，又不会把后续可靠性能力写成已经完成。
