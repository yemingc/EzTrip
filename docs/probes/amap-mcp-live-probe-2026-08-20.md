# 高德官方 MCP / REST 真实探针报告（2026-08-20）

## 结论

EzTrip 已用本地 Key 对高德官方 Streamable HTTP MCP 完成一次固定、低调用量的北京探针，并提交经过字段白名单和隐私脱敏的响应 fixture。结果证明 POI 搜索/详情、天气、距离、步行和公交路线可作为 V1 provider 的数据来源，但尚未证明旅行规划 Agent、酒店实时价格、预订能力或生产稳定性。

本次发现一个需要保留的协议边界：MCP `maps_weather` 返回未来四天预报，但没有 `reporttime` 和 `adcode`。EzTrip 因此仅对天气新鲜度保留高德 REST weather fallback，而不是把 REST 复制成第二套完整 provider。

## 固定输入与运行方式

- 城市：北京，adcode `110000`；
- POI：故宫博物院、天坛公园；
- 路线：由两次 POI 详情返回的坐标生成；
- MCP transport：Streamable HTTP；
- MCP SDK：Python `1.29.0`；
- MCP protocol：`2025-03-26`；
- 服务端：`amap-sse-server/1.0.0`；
- fixture：`evals/fixtures/amap/mcp-beijing-2026-08-20.v1.json`。

真实调用必须显式传入 `--live`，CI 只回放提交后的 fixture：

```powershell
Set-Location backend
uv run python -m scripts.run_amap_mcp_probe --live --write-fixture
```

命令只输出 operation、transport 和 latency，不打印原始响应、请求 URL 或 Key。Key 从 `.env` 注入，fixture 仅保留字段白名单，并再次执行 Secret/手机号/邮箱脱敏。

## 工具发现结果

2026-08-20 的 `list_tools` 实际返回 15 个工具：

| 类别 | 工具 |
|---|---|
| POI / 地理 | `maps_text_search`、`maps_around_search`、`maps_search_detail`、`maps_geo`、`maps_regeocode`、`maps_ip_location` |
| 距离 / 路线 | `maps_distance`、`maps_direction_walking`、`maps_direction_transit_integrated`、`maps_direction_driving`、`maps_direction_bicycling` |
| 天气 | `maps_weather` |
| 高德唤端 schema | `maps_schema_personal_map`、`maps_schema_navi`、`maps_schema_take_taxi` |

高德[官方能力说明](https://lbs.amap.com/api/mcp-server/summary)覆盖地点搜索、详情、天气、距离及多种路线；[快速接入文档](https://lbs.amap.com/api/mcp-server/gettingstarted)推荐 Streamable HTTP，并给出 `https://mcp.amap.com/mcp?key=...` 的客户端接入方式。EzTrip 在配置层只保存无 query 的 endpoint，运行时才在内存中附加 Key。

## 真实响应契约

| 调用 | 本次可用字段 | EzTrip 的使用边界 |
|---|---|---|
| `maps_text_search` | `id/name/address/typecode`；每个关键词返回多条候选 | 搜索只负责候选 ID，不假设包含可路线化坐标 |
| `maps_search_detail` | `location/address/type/rating/level/opening...` | 坐标用于路线；开放时间字符串可能不规则，必须归一化或降级展示 |
| `maps_weather` | `city` + 4 天 forecasts | 可发现天气风险；缺少 provider `reporttime/adcode`，不能单独证明新鲜度 |
| REST weather | `adcode/reporttime/casts` | 只补天气新鲜度，不扩展为平行 provider |
| `maps_distance` | `distance/duration` | 可构建候选间距离矩阵；数值仍需 typed normalizer |
| `maps_direction_walking` | 总距离、总时长、steps | 可用于日内转场校验；fixture 只保留前三个 step 作为契约样本 |
| `maps_direction_transit_integrated` | 多个方案的 duration、walking distance、segments | 可比较公交方案；当前 fixture 只保存摘要，不保存完整站点明细 |

本次两个景点详情的 `cost` 都为空，开放时间字段格式也不一致。这进一步说明 POI 详情不能当作酒店实时房价、房态、门票库存或可预订性来源。高德 REST 天气官方文档也明确列出 `reporttime` 和 `adcode` 字段，参见[天气查询 API](https://lbs.amap.com/api/webservice/guide/api/weatherinfo)。

## 单次运行观测值

以下只是一台本地开发机、一次顺序调用的观测，不是性能基准，也不能外推 p95：

| operation | latency |
|---|---:|
| REST weather preflight | 102 ms |
| 故宫文本搜索 / 详情 | 226 / 99 ms |
| 天坛文本搜索 / 详情 | 241 / 109 ms |
| MCP weather | 80 ms |
| distance | 86 ms |
| walking | 84 ms |
| transit | 299 ms |

本次 fixture 中，两点 distance type=1 返回 `5928 m / 1990 s`；步行首方案返回 `5508 m / 4406 s`；公交首方案返回 `3817 s`，其中步行 `2876 m`。这些数值只用于验证字段和回放测试，不应作为当前旅行建议。

## 错误与配额探针

使用固定假 Key（不是用户 Key）连接官方 MCP 时，服务端返回高德原始错误对象 `INVALID_USER_KEY / 10001`，而不是 MCP JSON-RPC 消息；Python MCP SDK 随后记录协议校验错误并等待，直到外层超时。这一失败促成两项实现约束：

1. 建立 MCP session 前，先用低成本 REST weather 请求验证 Key，并把 `10001` 分类为不可重试认证错误；
2. 对完整 MCP 探针设置总超时，避免协议外错误让调用无限等待。

当前错误分类覆盖认证、限流、超时、空结果、字段缺失和不可恢复错误。高德[错误码文档](https://lbs.amap.com/api/webservice/guide/tools/info)列出 `10001`（Key 不正确）、`10003`（日访问量超限）、`10004`（访问过于频繁）、`10015/10016`（超时/繁忙）等含义。

精确 QPS 与账号剩余配额依赖当前账号控制台，不能由公开文档或本次低流量探针推断。高德[流量限制说明](https://lbs.amap.com/api/webservice/guide/tools/flowlevel)要求以控制台为准；公开[个人开发者配额 FAQ](https://lbs.amap.com/faq/account/certification/39670)只能作为参考值，不能写成 EzTrip 账号的已验证额度。

## 可复现验证

离线 fixture tests 验证：

- 15 个工具和 9 条 capture call 的版本化结构；
- MCP weather 与 REST freshness 字段差异；
- POI 详情坐标、路线摘要和距离字段可解析；
- 请求参数与 endpoint 不包含 Key；
- fixture 不包含中国手机号或邮箱；
- AMap infocode 能映射为稳定的 provider 错误类别。

该探针是 EZ-004 的可行性证据。后续 EZ-005 已基于此 fixture 实现面向业务领域 DTO 的 provider adapter、fixture/live 双 transport、有界重试与 contract tests，详见 [AMap provider contract](../providers/amap-provider-contract.md)。Agent 和真实行程工作流仍未实现，因此不能声称“景点推荐 Agent 已接入高德”或“系统已能生成真实行程”。
