# PlannerContext 确定性编译层

`PlannerContext` 是 V1 规划流程的受控入口：它把已经通过 `TripRequest` 校验的结构化请求编译成日期、人数、预算、约束作用域、澄清问题和能力就绪状态。编译过程不调用大模型或外部 provider，相同输入会得到相同输入哈希、`context_id` 和结果。

## 为什么单独编译

自然语言解析、候选搜索和行程生成都会产生不确定性。如果直接让 Agent 自由解释“3000 元”“两个人”“少走路”，后续节点很难判断预算覆盖范围、住宿晚数和约束是否真的经过用户确认。`PlannerContext` 把可以机械计算的部分固定下来，并让无法安全推断的信息显式进入澄清队列。

当前边界是：

- 输入必须已经是有效的 `TripRequest`，本模块不负责把中文原话解析成结构化字段；
- 日期按包含首尾两天计算，住宿晚数固定为天数减一；
- 总预算只做参考尺度换算，不擅自分配到酒店、餐饮或门票；
- 房间数缺失时不根据人数猜测；
- 未确认约束不会被静默升级为已确认约束；
- 天气风险由后续天气 provider 主动发现，不属于用户追加输入；
- 当前 V1 城市目录只配置北京、上海和成都，用于证明行政区代码与 provider 能力门禁，不代表高德本身只支持这三座城市。

## 输出内容

- `input_request_sha256` 与 `context_id`：支持重放、缓存和 trace 对照；
- `DestinationContext`：保留用户输入、规范城市名、行政区代码和 provider 支持状态；
- `PartyPlanningContext`：计算总人数、住宿晚数和间夜数；
- `BudgetPlanningContext`：保留预算覆盖类别，并计算同行每日、人均全程和人均每日参考值；
- 三个约束桶：已确认硬约束、已确认软约束、未确认约束；
- `global_constraint_ids` 与逐日 `constraint_ids`：明确约束的全程或日期作用域；
- `clarifications`、`readiness`、`ready_capabilities` 和 `blocked_capabilities`：让 Graph 决定可以继续哪些工作，而不是只返回一个粗粒度布尔值。

## 澄清和能力门禁

| 情况 | 是否阻塞 | 被阻塞能力 |
| --- | --- | --- |
| 城市未进入 V1 配置目录 | 是 | 候选、住宿、天气、路线、最终定稿 |
| 缺少房间数，且预算包含住宿 | 是 | 住宿、预算校验、最终定稿 |
| 缺少房间数，但预算不含住宿 | 否 | 仅住宿搜索 |
| 缺少预算 | 否 | 仅预算校验；仍可产出不带预算保证的计划 |
| 未确认硬约束 | 是 | 最终定稿 |
| 未确认软约束 | 否 | 保留提问，不覆盖已确认条件 |

`ready` 表示没有澄清问题；`ready_with_questions` 表示可以继续部分或全部规划，但仍有可选问题；`needs_clarification` 表示至少存在一个会阻止关键能力的缺口。

## 可复现验证

```powershell
Set-Location backend
uv run python -m scripts.export_domain_schemas
uv run python -m scripts.export_planner_context_example
uv run pytest tests/test_planner_context.py tests/test_domain_contract_examples.py --no-cov
```

`docs/contracts/examples/planner-context.v1.json` 由已提交的 `trip-request.v1.json` 机械生成，测试会重新编译并做完整对象比较。下一阶段才会把该上下文接入 LangGraph 的澄清路由与候选搜索节点。
