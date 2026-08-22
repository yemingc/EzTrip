# Repair Router fixture baseline

EZ-304 用 9 条版本化案例验证 `ValidationIssue → 定向执行 → 再校验 → 停止` 闭环，而不是评估行程审美或实时 Provider 质量。

## 数据集

- suite：`evals/cases/repair-router/suite.v1.json`
- schema：`evals/schemas/repair-router-suite.v1.json`
- report：`evals/reports/repair-router-fixture.v1.json`
- case 数：9
- 执行模式：fixture，Router 0 次模型调用
- dataset SHA-256：`5876bc31d2b24a361964bebd1bc76d947c04052f29dc2c3fdf68f253f9d21c85`

覆盖五条单次成功修复（Route、Plan、Explore 营业证据、Explore 跨城 POI、Stay 跨城住宿）、一条不可自动掩盖的来源血缘错误、两条持续失败并在第二次停止的 Budget/Route 场景，以及一条硬预算费用下界超限并进入 HITL 的场景。

## 结果

- 9/9 cases passed；
- exact action + executed-node route：9/9，1.0000；
- retry bound respected：9/9；
- unaffected node reuse：9/9；
- deterministic replay：9/9；
- 总修复尝试：9；
- Router model calls：0；
- fixture executor delegated model-call accounting：5。

`unaffected node reuse` 要求每条 trace 的 `reused_nodes` 精确等于七个 pipeline 节点减去实际执行节点；Router 还会在代码层比较语义指纹，隐藏修改会被协议错误拒绝。`exact route` 同时要求 action 序列和每次 executed-node 序列与冻结期望一致。

## 边界

- fixture executor 模拟责任节点产物，不代表 live Agent 或高德 Provider 已在产品 Graph 中完成重跑；
- 成功案例是 orchestration contract 回归，不是自动修复成功率的生产估计；
- 预算缺价格与路线持续失败案例故意保留未解决结果，用于证明两次上限；
- `ASK_USER` 案例在任何 Agent 调用前停止，证明系统不会静默放宽用户硬预算；
- warning 不进入自动修复循环。

## 重放

```powershell
Set-Location backend
uv run python -m scripts.export_repair_router_schemas
uv run python -m scripts.run_repair_router_eval
uv run pytest tests/test_repair_router.py tests/test_repair_router_evaluation.py --no-cov
```
