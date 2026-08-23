# Live system comparison pilot protocol

EZ-502A 在任何真实模型调用前，冻结一份小规模、可复算、受预算约束的 repeated-live 协议。目标不是用 3 条案例证明系统已经泛化，而是回答一个更窄的问题：在既有代表性开发案例上，真实 DeepSeek 输出是否支持继续研究 Specialist 分工与 Hard Validator/Repair 的贡献。

## 为什么不直接把 30 条案例全部 live 跑多次

30-case fixture replay 适合验证确定性控制路径，但一次完整 live 三臂实验会放大模型调用、延迟和随机性。当前先运行 3 个不同城市与人群的代表案例，每条重复 2 次；如果信号明确，再决定是否建立新的 blind holdout 或扩大样本，而不是先消耗大量配额。

这 3 条案例来自既有 Prompt 开发集，因此数据角色固定为 `repeated_development_pilot`。它们不是未触碰 holdout，也不能产生泛化、真实用户成功率或生产质量结论。

## 案例与 trial

| 案例 | 侧重点 | 重复次数 |
|---|---|---:|
| 北京历史文化与轻步行三日 | 必去、历史兴趣、步行偏好 | 2 |
| 上海城市风光与本地美食两日 | 景点与餐饮候选取舍 | 2 |
| 成都亲子与熊猫基地三日 | 亲子画像、必去约束 | 2 |

共形成 6 个 paired trials。每个 trial 比较：

1. `single_agent_tools`：evaluation-only 完整 Single Agent，先从全部冻结候选中选择景点与住宿，再生成完整 `TripPlan`；
2. `product_graph_no_hard_gate`：读取 Specialist、Materials 与 Plan Agent 产生的初始草案，不允许生成阶段使用 Hard Validator 或 Repair；
3. `product_graph_bounded_repair`：从与上一 arm 完全相同的 Product 初始草案出发，再运行 Hard Validator 与最多两次模型修复额度。

两个 Product arms 必须共享同一次随机初始草案。否则两次独立生成的随机差异会被错误归因于 Hard Gate/Repair。

## 公平性控制

- 同一结构化 `TripRequest`、冻结 Provider catalogs、路线事实和预算材料；
- suite 与 dataset hash 冻结同一 `deepseek-v4-pro` 模型名称及 `temperature=0`；本地模型配置漂移会被 preflight 阻断，但仍不假设服务端绝对确定；
- 同一完整 `TripPlan` 输出契约与同一 post-run evaluator；
- 每个 trial 顺序执行，最大并行 live trial 为 1；
- 重复试验中不刷新 Provider 数据，不调用高德；
- 所有真实模型调用必须进入 LangSmith trace，并记录 trial、case、arm、node 和 dataset hash；
- 任一公平性不变量不满足时，停止运行且不生成相对提升结论。

高德 fixture 可以在实验前通过独立、一次性的流程刷新并脱敏，但刷新后的 hash 必须先冻结。模型对照运行中高德调用保持为 0，从而避免不同 arm 读到不同时间的数据，也把 Provider 波动与架构差异分开。

## 调用与 token 硬预算

每个 trial 的基础调用为：Single 候选选择 1 次、Single 规划 1 次、Product Explore 2 次、Stay 2 次、Plan 1 次，共 7 次。完整 Product arm 另有最多 2 次模型修复额度。

| 预算项 | 计算 | 上限 |
|---|---:|---:|
| 基础模型调用 | 6 trials × 7 | 42 |
| Repair 额度 | 6 trials × 2 | 12 |
| 总模型调用 | 42 + 12 | 54 |
| 基础 completion tokens | 6 × 6,900 | 41,400 |
| Repair completion 额度 | 6 × 2,400 | 14,400 |
| 总 completion tokens | 41,400 + 14,400 | 55,800 |
| 高德调用 | 冻结 fixture | 0 |

55,800 是输出 token 的最坏情况 ceiling，不是预计消耗，更不是货币成本估算。runner 必须在发起每次调用前检查剩余额度，达到 54 次调用或 token 上限后 fail closed；实际报告应记录 input/output tokens、wall-clock 和失败，不用预算上限冒充实际用量。

## 计划记录的结果

- 人工标签下的候选相关性与标签组覆盖；
- 完整方案能否 finalization，以及 Hard Validator issue 集；
- candidate/source grounding、路线边血缘与必去约束；
- paired repair action、停止原因和每次修复前后 issue diff；
- 同一案例两次运行的一致性与失败类型；
- 每个 arm 的实际模型调用、input/output tokens、wall-clock p50/p95；
- Provider 调用固定为 0，并在报告中明确显示。

只有三案例、指定日期、指定模型和冻结数据范围内的 paired 结果可以写入项目说明。即使完整 Product arm 更好，也不能表述为“多 Agent 普遍优于单 Agent”；如果 Specialist 没有 lift，同样应如实保留。

## 零调用预检

```powershell
Set-Location backend
uv run python -m scripts.plan_live_system_comparison
```

该命令只加载本地 suite、source fixtures 与 `.env` 配置状态，输出 dataset hash、模型名称、调用预算和依赖是否就绪。它不构造 API client，不调用 DeepSeek、高德或 LangSmith，也不会输出 key 内容。

EZ-502B 才会实现真实 runner。runner 必须要求显式 `--live`，运行前再次打印 dataset hash、6 trials、54 次硬上限和 0 次高德调用，并要求调用方明确确认；没有显式 live 参数时只能执行 fake/contract 测试。
