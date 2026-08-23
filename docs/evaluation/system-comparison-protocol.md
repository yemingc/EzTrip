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

## Fixture control-path replay 结果

EZ-501B 已实现三组同构 runner。它读取相同的冻结工具快照，由单 Agent 显式查看全部住宿候选并选择锚点，三个 arm 生成完整 `TripPlan` 后统一交给 `hard-trip-plan-validator-v1`。报告固定写入 [`evals/reports/system-comparison-fixture.v1.json`](../../evals/reports/system-comparison-fixture.v1.json)。

| arm | 可评估 case | 可定稿 | finalization rate | 模型调用 | Provider 调用 |
|---|---:|---:|---:|---:|---:|
| `single_agent_tools` | 28 | 4 | 14.29% | 28 | 435 |
| `product_graph_no_hard_gate` | 28 | 4 | 14.29% | 144 | 435 |
| `product_graph_bounded_repair` | 28 | 20 | 71.43% | 186 | 638 |

配对结果中，Single Agent 到无 Hard Gate Product Graph 是 0 个改善、0 个恶化、28 个不变；无 Hard Gate 到完整 Product Graph 是 16 个改善、0 个恶化、12 个不变，即 `+0.5714`。完整链路保持冻结库存：4 个无需修复、16 个 repaired、1 个 waiting-for-user、7 个 unresolved、2 个 blocked-before-plan，30/30 与预期一致。

这是一份 `fixture_control_path_replay`，只允许声称：在这批开发集故障注入上，生产 Hard Validator 与有界 Repair 恢复了 16 条原本不可定稿的草案。它不允许声称 Specialist Agent 提升了模型规划质量，也不代表真实用户成功率。Single 与无 Gate Product arm 在 fixture 中使用同一确定性排程策略，因此二者相同是预期结果；住宿路线矩阵也只覆盖最终锚点，不构成酒店排序评测。

报告没有调用 DeepSeek、高德或 LangSmith。Single arm 的 fixture token 记录完整，总计 6720；Product fixture 未完整记录所有 Specialist token，延迟也不是 live wall-clock，因此对应 token 总量和 p50/p95 均明确留空，而不是拿不完整数字比较。

## 验证

```powershell
Set-Location backend
uv run python -m scripts.export_comparison_eval_schemas
uv run python -m scripts.run_system_comparison_eval
uv run pytest tests/test_comparison_evaluation_contract.py tests/test_system_comparison_evaluation.py --no-cov
```
