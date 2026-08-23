# Repair Router V1

EZ-304 把 Hard Validator 的 typed `ValidationIssue` 变成有界、可审计的修复循环：

```python
await run_repair_router(request, plan, materials, opening_hours, executor)
```

Router 本身不调用 LLM，也不决定新景点或新行程内容。它只负责选择最小 `repair_action`、调用注入的责任节点 executor、重新执行 Hard Validator，并记录 issue diff、plan diff、产物哈希、调用计数与复用节点。业务 Agent 和 Provider 仍通过 executor 边界接入。

## 路由与依赖范围

| repair action | 必须执行 | 允许按依赖继续执行 | 必须复用 |
|---|---|---|---|
| `rerun_constraint` | Constraint | Explore、Stay、Route、Budget、Plan | Weather |
| `rerun_explore` | Explore | Route、Plan | Constraint、Stay、Weather、Budget |
| `rerun_stay` | Stay | Route、Plan | Constraint、Explore、Weather、Budget |
| `rerun_route` | Route | Plan | Constraint、Explore、Stay、Weather、Budget |
| `recalculate_budget` | Budget | Plan | Constraint、Explore、Stay、Weather、Route |
| `replan_day` | Plan | 无 | 其他全部节点 |

“允许”不等于每次都要执行。例如营业时间证据缺失但候选集合不变时，Explore 可以只刷新证据，不必重跑 Route 和 Plan。相反，如果 Explore 换了 candidate ID，下游 executor 应继续重建路线和排程，并把三个实际节点都写入 trace。

Router 对成功结果重新计算 Constraint、Explore、Stay、Weather、Route、Budget、Plan 七个语义指纹。如果 executor 修改了一个标记为“复用”的节点，或在 `rerun_route` 中偷偷改了 Stay，Router 会抛出 `RepairRouterProtocolError`，拒绝写回该结果。失败尝试必须丢弃部分输出，继续使用修复前 checkpoint。

## 停止规则

Router 只自动处理 `severity=error`：

1. 没有 error 时返回 `already_finalizable` 或 `repaired`；warning 保留给 UI/Review 展示，不触发自动循环；
2. `ask_user` 或 `requires_user_confirmation=true` 立即返回 `waiting_for_user`，不调用 Agent；
3. `repair_action=none` 等程序不变量错误返回 `unresolved/unrepairable_issue`；
4. 自动 action 按 Constraint → Explore → Stay → Route → Budget → Plan 的上游顺序处理；
5. 同一 action 最多两次，第三次执行前返回 `retry_limit_reached`。

上游优先的原因是上游修复可能让下游 issue 消失。例如跨城 POI 与缺路线同时存在时，先修 Explore，再重新校验；只有路线问题仍存在才调用 Route。

## Trace 契约

每个 `RepairAttemptTrace` 包含：

- 全局 attempt index 与该 action 的第几次尝试；
- 触发 rule code、action、责任节点；
- 实际执行节点与复用节点；
- 修复前后 error code、已解决与新增 issue；
- materials、plan、opening-hours 的前后 SHA-256；
- changed dates、候选增删与费用上下界变化；
- 下游模型/Provider 调用数和稳定错误码。

`RepairRouterResult` 保存初始/最终 Validator 报告、最终三个产物、每类重试计数、未解决 error 和总调用数。Router 的模型调用数始终为 0；executor 内 Agent 的调用单独计入 delegated calls。

## 当前事实边界

- 已实现真实的路由、重试、停止、校验、产物保护与 trace 代码；
- 9-case 隔离回归仍使用注入的 fixture executor 模拟责任节点返回，用于证明 Router 编排契约；
- Product Graph V2 已注入真实产品 executor：Explore/Stay 修复会重跑对应 Agent、Route 和 Plan，Route/预算/Plan 修复只执行允许的责任链，并把实际模型/Provider 调用计入 trace；
- 默认产品 fixture 的营业时间错误由确定性 `replan_day` 修复，天坛从 09:00 移到已验证的 10:00 开放窗口，未受影响节点保持复用且不调用模型/Provider；
- 成功 fixture 证明 orchestration contract，不证明实时高德、营业时间或价格来源可用；
- 预算费用下界超限必须进入 HITL，Router 不会擅自提高预算或删除硬约束；
- 天气主动触发和受影响日期的局部修复属于 EZ-305。

## 重放

```powershell
Set-Location backend
uv run python -m scripts.export_repair_router_schemas
uv run python -m scripts.run_repair_router_eval
uv run pytest tests/test_repair_router.py tests/test_repair_router_evaluation.py --no-cov
```
