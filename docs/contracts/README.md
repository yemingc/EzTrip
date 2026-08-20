# EzTrip domain contracts

`domain-contracts.v1.json` 是从后端 Pydantic 模型机械导出的 V1 JSON Schema bundle。它冻结 Agent、确定性节点、provider adapter 与未来 API 之间的字段边界，不代表这些业务能力已经全部实现。

当前契约明确以下事实：

- V1 只支持中国单城市、2–5 个自然日、人民币预算；
- `PlannerContext` 记录对 `TripRequest` 的唯一确定性解释、输入哈希、澄清问题和能力就绪状态；
- 金额使用 `Decimal` 语义，`CostItem` 可确定性重算最小/最大总额；
- POI、住宿、天气和路线数据携带 provider、数据模式与获取时间；
- `WeatherRisk` 只能由 live/fixture 天气工具数据产生，不能伪装成用户追加输入；
- 住宿候选不包含实时房态或可预订承诺，价格只能缺省或明确标记来源的估算；
- `ValidationIssue` 显式记录责任节点、证据、修复动作和是否需要用户确认；
- `PlanVersion` 保存约束哈希、工具快照、模型/Prompt 版本和变更范围。

示例：

- `examples/trip-request.v1.json`：北京三日请求；
- `examples/planner-context.v1.json`：由上述请求机械编译的规划上下文；
- `examples/trip-plan.v1.json`：带 provider 来源和费用台账的结构化计划；
- `examples/validation-issue.v1.json`：不可满足预算冲突。

重新导出：

```powershell
Set-Location backend
uv run python -m scripts.export_domain_schemas
uv run python -m scripts.export_planner_context_example
```

测试会校验示例可解析、序列化可往返，检查提交的 schema bundle 与代码生成结果一致，并重新编译 `TripRequest` 验证 `PlannerContext` 示例没有漂移。
