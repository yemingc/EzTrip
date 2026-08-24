# ADR-009：城市身份由 Provider 解析并在规划前消歧

- 状态：Accepted
- 日期：2026-08-24

## 背景

早期产品演示由前端固定提交北京，两日 fixture 也只证明单一城市链路。若把北京、上海、成都扩展成静态下拉框或后端白名单，既无法支持中国用户自由输入目的地，也会把测试数据覆盖误写成产品覆盖。另一方面，规范城市名、行政层级和高德 `adcode` 是外部事实，不能交给 LLM 猜测。

## 候选方案

1. 在代码中维护允许城市和行政区代码表；
2. 让 Request Intake/LLM 直接输出城市名与 `adcode`；
3. 建立 typed `CityResolverProvider`，在任务创建前解析候选并由用户确认歧义结果。

## 决策

采用方案 3：

- live resolver 使用高德 REST 地理编码返回规范名称、行政层级、六位 `adcode`、中心点和来源；
- fixture resolver 只覆盖北京、上海、成都以及 `朝阳` 重名案例，用于无 Key 的 CI/E2E；
- `resolved`、`ambiguous`、`no_result`、`unsupported` 和 Provider/configuration failure 保持不同语义；
- 重名行政区必须先选择候选，未确认时不创建 planning task；
- planning executor 必须重新解析并校验客户端提交的 `selected_destination_adcode`，再把统一城市身份交给 Explore、Stay、Weather、Route 和 Plan 链路；
- 前端选择 live 模式即为单次请求的显式外部调用授权，不再叠加部署级布尔开关；Key 仍只保存在服务端，缺失凭据时在 checkpoint/付费规划前 typed fail；
- live 产品范围由 Provider 可解析性决定，不把 fixture 城市当成产品白名单；一次任务仍只支持一个国内目的地、2–5 天。

## 后果

- 可输入任意国内城市不再依赖手工维护数百条静态目录；
- 城市重名、无结果、live 未启用和外部服务失败都能在付费规划前暴露；
- 北京、上海、成都的确定性回归与全国 live 路径被明确分层；
- 每个 Provider 使用同一个经过确认的名称和 `adcode`，降低 POI、天气与路线串城风险；
- 仍需用非 fixture 城市 live canary 验证真实覆盖，不能据此宣称“所有城市规划质量已验证”。

## 重新评估条件

当高德地理编码对重名行政区的召回不足、配额或 SLA 不满足产品要求时，保留 `CityResolverProvider` 契约并替换/组合行政区数据源。跨城市行程需要新的多目的地、城际交通和联合预算契约，不在本 ADR 上静默扩展。

官方字段与工具边界参考[高德地理编码 API](https://lbs.amap.com/api/webservice/guide/api/georegeo/)和[高德 MCP Server 概述](https://lbs.amap.com/api/mcp-server/summary)。
