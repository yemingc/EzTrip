# System comparison V1

这 30 条 case 冻结 EzTrip 消融评测的输入与公平性协议。EZ-501B 已完成离线三组 runner，机器可读结果位于 [`../../reports/system-comparison-fixture.v1.json`](../../reports/system-comparison-fixture.v1.json)。

三组对照必须使用：

1. `single_agent_tools`：一个完整 Planner Agent，使用同一工具事实并输出完整 `TripPlan`；
2. `product_graph_no_hard_gate`：Product Graph 在 Hard Validator 前停止；
3. `product_graph_bounded_repair`：当前完整 Product Graph、Hard Validator 与 Repair Router。

三个 arm 必须共享结构化请求、Provider fixture、完整 `TripPlan` 输出契约、post-run evaluator 和 live 模型名称。`product_graph_no_hard_gate` 不能在生成阶段使用门禁，但其草案仍由同一个只读 evaluator 在运行结束后评分。只在三个 arm 都具备同等输入能力的 case 上计算相对指标。

历史 `single-planner-v1` 只消费单个候选并输出部分 `DayPlan`，不能作为这里的单 Agent arm，否则输出范围和工具事实不一致，比较结论无效。

数据集是开发集回归，不是未触碰 holdout。20 条 standard 覆盖正常链路及可修复的路线、营业时间、跨城和来源问题；10 条 hard 覆盖不可修复血缘、硬预算事实缺口、HITL、有界重试、Provider 超时和能力边界。

当前 fixture 结果是 Single 4/28、无 Hard Gate Product 4/28、完整 Product 20/28；后两组的配对差值为 16 个改善、0 个恶化。它只测量相同草案策略下 Hard Validator + bounded Repair 的控制路径恢复能力。Single 与无 Gate Product 相同，因此不能从这份报告声称 Specialist Agent 带来模型质量提升；报告也没有发起 DeepSeek、高德或 LangSmith live 调用。
