# ADR-003：高德官方 MCP 优先，自有 typed provider 隔离协议

- 状态：Accepted
- 日期：2026-08-20

## 背景

EzTrip 面向中国用户，需要 POI、天气、距离和路线数据。高德官方 MCP 能快速提供这些能力，但 MCP 返回结构、网络错误和配额不应直接渗透到领域模型或 Agent Prompt；同时，高德 POI 不等同于酒店实时房价和库存。

## 候选方案

1. Agent 直接调用高德 MCP，并把原始结果写入 GraphState；
2. 业务只依赖自有 typed provider port，官方 MCP 为默认 live adapter，fixture 为 CI adapter；
3. V1 自建 MCP Server，再由应用调用；
4. 只使用高德 REST API。

## 决策

采用方案 2：

- 默认 live 入口使用高德官方 Streamable HTTP MCP；
- adapter 把 MCP 结果归一化为带 `source`、`retrieved_at` 和稳定错误分类的领域 DTO；
- CI、评测和故障注入使用版本化 fixture，不依赖真实 Key 或网络；
- 只有当 MCP 缺失必要字段时，才在同一 provider port 后增加 REST fallback；
- V1 不自建 MCP Server，因为当前没有跨应用复用或独立部署需求。

`AMAP_MAPS_API_KEY` 只能从运行环境注入，不拼接到可记录的 URL，不写入 trace、fixture 或异常消息。

## 后果

- 可以替换传输协议和 provider，而不重写 Agent 和领域模型。
- contract tests 能回放成功、超时、限流、字段缺失和无结果场景。
- 需要额外维护 adapter 与 fixture，但这正是工具可靠性和可测试性的核心证据。

## 2026-08-20 探针验证

固定北京 live probe 实际发现 15 个工具，并验证了 POI 搜索/详情、天气、距离、步行和公交路线。`maps_weather` 没有返回 `reporttime/adcode`，因此触发本 ADR 所述的最小 REST weather fallback；POI 的 `cost` 为空且开放时间格式不规则，不能作为酒店/门票库存或可信价格。

完整字段、延迟、错误和配额边界见[高德官方 MCP / REST 真实探针报告](../probes/amap-mcp-live-probe-2026-08-20.md)。

## 2026-08-20 adapter 验证

EZ-005 已实现 `AmapTravelDataProvider`、请求级 live MCP client 和版本化 fixture client。两种 transport 通过同一组 POI、天气风险和路线 contract tests，并都输出 `CandidatePOI`、`WeatherRisk` 与 `RouteLeg`。只有超时和限流允许有界重试；认证、空结果、字段缺失和协议错误保持 typed failure。

实现与当前限制见 [AMap provider contract](../providers/amap-provider-contract.md)。

## 重新评估条件

出现以下情况之一时重新评估：官方 MCP 无法提供稳定字段；配额或延迟不满足演示；需要给多个独立应用复用同一 provider；REST fallback 逻辑超过 adapter 可维护范围。

## 参考

- [高德 MCP Server](https://lbs.amap.com/api/mcp-server/summary)
- [高德 MCP 快速接入](https://lbs.amap.com/api/mcp-server/gettingstarted)
