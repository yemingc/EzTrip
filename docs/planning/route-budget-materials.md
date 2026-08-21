# 路线矩阵与确定性预算材料层

EZ-301 把 `SpecialistFanoutResult` 转换成 Plan Agent 可消费的 `PlanningMaterialBundle`。该层不调用新的 LLM，也不生成逐日行程：它先缩小候选范围，再并发查询候选间路线，并把用户总预算拆成可审计的类别目标。

```mermaid
flowchart LR
    S[SpecialistFanoutResult] --> L[PlanningShortlist]
    L --> R[RouteMatrix]
    S --> B[BudgetAllocation]
    R --> M[PlanningMaterialBundle]
    B --> M
```

## 候选范围与路线矩阵

`planning-shortlist-v1` 按 Agent 已给出的 rank 取前四个 POI，并取排名第一的 Stay 作为住宿锚点；未进入路线计算的 candidate ID 会显式保存在 `omitted_*_ids`。这避免候选数量增长时产生无界的外部调用。

对于 `n` 个 POI，矩阵包含 `n × (n - 1)` 条 POI 有向边；存在住宿锚点时再加入 `2n` 条 Stay ↔ POI 边。V1 最多四个 POI，因此最多 20 条边。路线固定为公交 `transit`，默认并发上限为 4，每一条边恰好调用一次最小 `RouteProvider.get_route()` 接口。

- capability 被阻断时返回 `blocked/capability_blocked`，Provider 零调用；
- 没有 Explore 候选时返回 `unavailable/no_explore_candidates`；
- 只有一个 POI 且没有住宿锚点时返回 `not_required/insufficient_candidate_pair`；
- 单边失败保留为 `partial`，所有边失败为 `failed`，成功边不被删除；
- Provider timeout 会保留 `provider/timeout/retryable=true`；未知依赖错误与 Provider 错误分开；
- Provider 返回不同端点属于协议违规，会抛出 `PlanningMaterialProtocolError`，不能降级成普通超时。

## 预算分配不是价格预测

`budget-allocator-v1` 使用普通 `Decimal` 代码和版本化策略 `cn-city-trip-weights-v1`。基准权重为住宿 35%、交通 20%、餐饮 25%、门票 10%、活动 5%、其他 5%；如果用户预算不覆盖全部类别，只在 included categories 内重新归一化。金额先向下取到分，再用 largest-remainder 顺序补足余分，保证所有目标金额与总预算精确相等。

每个类别还带一个可解释的参考尺度：

| 类别 | 参考尺度 |
|---|---|
| 住宿 | `room_night` |
| 交通、餐饮 | `party_day` |
| 门票、活动 | `traveler_trip` |
| 其他 | `party_trip` |

这些金额是给后续排程和 Validator 使用的目标 envelope，不是高德或 OTA 价格，不代表“按这个金额一定能成行”。没有预算时返回 `not_requested/missing_budget`；住宿包含在预算但缺少房间数时返回 `blocked/missing_rooms`，不会猜一间房或继续住宿算术。

## Bundle 状态

`PlanningMaterialBundle` 同时保留完整 specialist 结果、shortlist、路线矩阵和预算分配，并校验 request/context/data-mode 血缘。只有 specialist 完整、路线完整且预算已分配时才是 `ready`；可继续但有缺口时为 `partial`；Explore 不可用或路线能力被阻断时为 `blocked`。缺口以稳定 issue code 暴露，而不是自由文本。

## 验证

```powershell
Set-Location backend
uv run pytest tests/test_planning_materials.py tests/test_planning_material_eval.py --no-cov
uv run python -m scripts.run_planning_material_eval
```

当前 fixture 路线只用于验证编排、来源、并发和失败契约。实时高德路线仍由既有 adapter/probe 单独验证；本阶段没有新增 DeepSeek 调用，也不产生 LangSmith trace。
