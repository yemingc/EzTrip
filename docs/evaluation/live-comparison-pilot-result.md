# DeepSeek live system comparison pilot result

EZ-502B 在 2026-08-23 按冻结协议执行了 3 个既有开发案例、每个案例 2 次重复的 paired live pilot。该实验使用 `deepseek-v4-pro`、`temperature=0`、冻结 Provider catalogs 和本地路线 fixture；模型调用进入 LangSmith，高德及其他外部旅行 Provider 调用为 0。

原始机器可读报告位于 [`evals/reports/deepseek-live-system-comparison-pilot-2026-08-23.json`](../../evals/reports/deepseek-live-system-comparison-pilot-2026-08-23.json)。它通过版本化 Pydantic/JSON Schema 校验，并保存每次物理模型调用、token usage、稳定错误码、trial trace ID、输入 catalog hash 与计划 hash；不保存 API Key 或原始自然语言回答。

## 执行结果

| 项目 | 实际值 |
|---|---:|
| Paired trials | 6/6 完成 |
| DeepSeek 物理调用 | 42/54，42 成功、0 失败 |
| Prompt tokens | 65,695 |
| Completion tokens | 9,398/55,800 ceiling |
| Total tokens | 75,093 |
| LangSmith trial traces | 6/6 |
| 高德调用 | 0 |
| Repair 模型调用 | 0 |

55,800 是 completion token 上限，不包含 prompt tokens，因此 total tokens 大于该数字不表示越过预算。由于 6 个 Product 初始草案都已通过 Hard Validator，本次没有触发 Repair；42 次基础调用低于 54 次最坏情况上限。

## 三臂结果

| 指标 | Single Agent + tools | Product，无 Hard Gate | Product + bounded Repair |
|---|---:|---:|---:|
| 执行成功 | 6/6 | 6/6 | 6/6 |
| 可 finalization | 6/6 | 6/6 | 6/6 |
| 标签候选相关性 | 1.0000 | 1.0000 | 1.0000 |
| 必需标签组覆盖 | 1.0000 | 1.0000 | 1.0000 |
| 合法住宿选择 | 6/6 | 6/6 | 6/6 |
| Grounding / route lineage | 1.0000 / 1.0000 | 1.0000 / 1.0000 | 1.0000 / 1.0000 |
| 两次重复计划完全一致的案例 | 2/3 | 3/3 | 3/3 |
| Logical model calls | 12 | 30 | 30 |
| p50 累计模型调用延迟 | 7,219 ms | 19,535 ms | 19,535 ms |
| p95 累计模型调用延迟 | 8,364 ms | 20,497 ms | 20,497 ms |

两个 Product arm 逐 trial 共享同一初始 `TripPlan` SHA-256。完整 Product arm 本轮没有修复调用，因此与无 Gate arm 的结果和成本相同；这不是两次重复付费执行。

按物理调用归属统计，Single 路径 12 次调用消耗 29,497 total tokens，Product 初始路径 30 次调用消耗 45,596 total tokens。Product 在本小样本中以更多调用和更高累计模型延迟换来 3/3 exact-repeat consistency；Single 在成都案例的两次完整计划 hash 不同，因此为 2/3。这个一致性差异只有一个案例，属于观察信号，不是统计结论。

## 能回答什么

- 三类代表性开发请求都能让 Single 与 Product 生成 grounded、route-backed、可 finalization 的完整 `TripPlan`；
- 多 Agent Product 路径的 Explore、Stay、Plan 分工能在真实模型下按共享契约完成，6 次重复没有执行失败；
- runner 实际执行了 fail-closed 调用预算、共享 Product 初稿、逐调用 journal、token/latency 归属和 LangSmith trial tracing；
- 在这三个 clean development cases 上，没有观察到 finalization、候选相关性或约束覆盖提升；观察到的窄信号是 Product exact-repeat consistency 为 3/3，Single 为 2/3。

## 不能回答什么

- 案例来自既有 Prompt 开发集，不是 blind holdout，不能声称泛化或真实用户成功率；
- Provider 数据是 fixture，不能声称实时景点、天气、路线、酒店价格、房态或预订质量；
- clean cases 没有触发 Repair，因此本轮不能用 live 数据证明 Hard Validator/Repair 提升；该控制路径只有 30-case 故障注入 fixture replay 证据；
- `temperature=0` 不保证服务端完全确定，2 次重复不足以估计稳定性分布；
- 累计模型调用延迟是 arm 内各调用 latency 之和，不是并行 Product Graph 的端到端响应时间。

## 复现边界

零调用预检：

```powershell
Set-Location backend
uv run python -m scripts.plan_live_system_comparison
```

真实运行必须同时显式确认 live 模式和 54 次最坏情况调用上限：

```powershell
uv run python -m scripts.run_live_system_comparison_pilot `
  --live `
  --confirm-max-model-calls 54
```

缺少任一参数时，CLI 会在加载 Settings 或构造外部 client 前退出。CI 只执行 fake/fixture contract tests，不运行该 live 命令。
