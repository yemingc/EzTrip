# System comparison V1 protocol

EZ-501A 冻结 30 条 system-level development regression case，用于后续回答一个可证伪的问题：在相同请求、相同工具事实、相同模型名称和相同完整输出契约下，Product Graph 的 Hard Validator 与有界 Repair 是否比单 Agent 基线更可靠，代价是多少。

## 三个 arm

| arm | 运行边界 | 目的 |
|---|---|---|
| `single_agent_tools` | 单个完整 Planner Agent 使用冻结工具事实并输出 `TripPlan` | 提供公平的单 Agent + tools 基线 |
| `product_graph_no_hard_gate` | Specialist、Materials、Plan 已运行，但在 Hard Validator 前停止 | 隔离专业分工本身与最终门禁的贡献 |
| `product_graph_bounded_repair` | 完整 Product Graph、Hard Validator、Repair Router | 评估最终方案的可靠性、恢复成本与停止行为 |

三个 arm 产出的 `TripPlan` 都会交给同一版本的只读 post-run evaluator。`product_graph_no_hard_gate` 只是不能在生成阶段使用 Hard Validator 拦截或修复，不代表它在实验结束后不接受同一套规则打分。

历史 `single-planner-v1` 不能复用为第一个 arm。它只消费单个必去候选并输出部分 `DayPlan`，而 Product Graph 消费景点、住宿、天气、路线和预算材料并输出完整 `TripPlan`。直接比较会混淆输入信息量、输出范围与架构差异。

## 公平性不变量

三个 arm 必须共享：

- 同一结构化 `TripRequest`；
- 同一版本化 Provider fixture 与 tool snapshot hash；
- 同一完整 `TripPlan` 输出契约；
- 同一版本的 post-run evaluator；
- live 对照中的同一模型名称；
- 同一 case eligibility denominator。

任一条件不满足时，不生成相对提升结论。数据集明确标记为 `development_regression`，因为其场景和预期停止行为由项目开发者设计，不是未触碰测试集。

## 30-case 库存

- 20 条 standard：4 条正常链路，16 条路线缺失/血缘、营业时间、跨城 POI/住宿和转场窗口等可修复场景；
- 10 条 hard：2 条不可修复候选血缘、2 条硬预算事实缺失、1 条预算下界 HITL、2 条持续约束失败、1 条路线 Provider 超时、1 条不支持城市、1 条无可行营业窗口；
- 来源覆盖：北京历史、北京餐饮、上海城市风光、成都亲子，以及路线超时和南京能力边界；
- 预期完整方案结果：4 个无需修复、16 个 repaired、1 个 waiting-for-user、7 个 unresolved、2 个 blocked-before-plan。

Suite 的 SHA-256 同时覆盖 30 条 case、引用的 Plan Agent source cases 和其下游 Explore/Stay Provider fixtures；修改引用事实也会改变 comparison dataset hash。

## 后续报告指标

runner 完成后按 arm 聚合：

- 共享 eligible cases 的 hard-constraint finalization rate；
- candidate grounding、source traceability、route lineage；
- repaired / waiting / unresolved / blocked 数量和平均修复次数；
- 模型调用、Provider 调用、token、p50/p95 latency；
- 每个 case 的工具快照一致性与 deterministic replay。

本阶段只冻结协议、数据集、schema 和引用完整性测试，不声称已完成三组 runner，不发布多 Agent 提升值，也不产生 DeepSeek 或高德调用。

## 验证

```powershell
Set-Location backend
uv run python -m scripts.export_comparison_eval_schemas
uv run pytest tests/test_comparison_evaluation_contract.py --no-cov
```
