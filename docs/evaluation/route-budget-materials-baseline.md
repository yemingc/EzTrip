# Planning materials V1 baseline

## 冻结套件

`evals/cases/planning-materials/suite.v1.json` 固化五条材料编排案例，并把自身与所引用的 specialist-fanout 数据集共同计算 SHA-256：`eeeff5b8638b7aba7e981195f793d5cc1f043a28c4fde2c7ba76762d76b70e7f`。

| Case | 预期材料状态 | 关键边界 |
|---|---|---|
| 完整路线与全类别预算 | `ready` | 3 POI + 1 Stay 形成 12 条有向边 |
| 单路线 timeout | `partial` | 1 条 typed failure，保留其余 11 条边和预算 |
| 未设置预算 | `partial` | 路线继续，预算不能伪装为 0 元满足 |
| 住宿预算缺少房间数 | `partial` | 住宿预算算术阻断，6 条 POI 路线继续 |
| 未配置城市 | `blocked` | 路线 Provider 零调用 |

## 可重放结果

提交的 `evals/reports/planning-materials-fixture.v1.json` 结果如下：

| Metric | Result |
|---|---:|
| Cases | 5/5 passed |
| Route edges | 42/42 |
| Route Provider calls | 42 |
| Typed retryable route timeouts | 1/1 |
| Allocated budgets with exact cent sum | 2/2 |
| Blocked cases with zero route calls | 1/1 |
| Cases respecting concurrency ≤ 4 | 5/5 |
| Cases with source-traceable successful routes | 4/4 eligible |

完整与单边超时案例都使用 3000 CNY：全类别预算得到 1050/600/750/300/150/150；排除住宿和其他时，剩余权重重新归一化为交通 1000、餐饮 1250、门票 500、活动 250。两者都以分为单位精确回到 3000，而不是用浮点数近似。

## 证据边界

这套结果支持以下窄结论：候选规模有界；有向路线按固定次序并发查询；单边 timeout 可类型化降级且保留成功边；能力阻断发生在 Provider 调用前；预算按版本化权重和人数/天数/间夜尺度精确分配。

它不证明路线是当前真实高德结果、预算符合市场价格、多 Agent 比单 Agent 的最终行程更好，或系统已经生成完整 `TripPlan`。本阶段故意使用 fixed models 和 route fixture 来隔离确定性材料层，因此 5/5 不是模型准确率，也没有新增 token 或在线延迟指标。真正的最终计划质量对照需要后续 Plan Agent 让 single/multi 两条路径输出同一个 TripPlan schema，再通过同一 Validator 与冻结标签评估。
