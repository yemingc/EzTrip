# Specialist fan-out V1 baseline

## 冻结数据集

`evals/cases/specialist-fanout/suite.v1.json` 固化五条编排案例，并把自身与所引用的 Explore/Stay fixture 一起计算 SHA-256：`c2386285bd0e0648b1d1c7f68c612b47a2be3276f3d1b8f3edf3a0fddbf6e8b5`。

| Case | 预期 |
|---|---|
| 三分支完整完成 | `complete`，证明三个 Provider 同时在途和有序无覆盖合并 |
| Explore Provider timeout | `partial`，Stay/Weather 保留 |
| 缺少房间数 | `partial`，Stay 零调用跳过，Explore/Weather 保留 |
| Weather Provider timeout | `partial`，Explore/Stay 保留 |
| 不支持城市 | `blocked`，三个分支均在模型/Provider 调用前停止 |

五条请求都没有要求查询天气。北京案例仍由 `PlannerContext` 能力路由主动调用天气；不支持城市则明确跳过，避免无意义调用。两个超时是受控的 fixture Provider 故障注入。

## 可重放结果与点时结果

离线 fixed-model 报告 `evals/reports/specialist-fanout-fixture.v1.json` 为 5/5 cases、15/15 分支状态，适合 CI 回归。

2026-08-21 的 `deepseek-v4-pro` + LangSmith 点时报告位于 `evals/reports/deepseek-specialist-fanout-baseline-2026-08-21.json`：

| Metric | Result |
|---|---:|
| Cases | 5/5 passed |
| Branch status expectations | 15/15 |
| Exact ordered three-branch merges | 5/5 |
| Typed Provider failures | 2/2 |
| Successful branches preserved under injected failure | 4/4 |
| Proactive weather calls | 4/4 eligible cases |
| Blocked cases with zero calls | 1/1 |
| Parallel Provider-entry controls | 1/1 |
| Source-traceable eligible case outputs | 4/4 |
| Model calls / usage records | 13 / 13 |
| Fixture Provider calls | 23 |
| Prompt / completion / total tokens | 15,466 / 3,391 / 18,857 |
| Fan-out latency p50 / p95 | 7,044 / 8,984 ms |

Provider 调用数不是固定的三倍案例数：Explore 和 Stay 会先让模型生成 1–4 条检索策略，再为每条策略调用 fixture Provider。天气始终每个 eligible case 一次 Provider 调用、零次模型调用。token 汇总包含 Explore 超时案例中“模型查询策略已完成、Provider 随后失败”的 usage，因此不会因部分失败低报已发生的模型成本。

## 证据边界

该基线支持以下窄结论：三个专业分支按能力并行运行；reducer/merge 不覆盖状态；单分支故障可类型化降级并保留其他结果；天气无需用户触发；完整 checkpoint 可恢复而不重放；模型调用和 token 可审计。

它不支持“多 Agent 比单 Agent 行程更好”。当前 fan-out 输出是专业信息包，既没有与单 Agent 生成同构 `TripPlan`，也没有路线、营业时间、完整费用和用户满意度标签。有效的 single-vs-multi 对比要等 EZ-301 把两条路径都接到同一最终计划 schema 和相同 Validator 后再做。

fixture 候选与天气风险不代表实时高德召回、新鲜天气、酒店价格、库存或生产 SLA。五条开发案例也不是未见 holdout；报告是点时回归证据，不是泛化准确率。
