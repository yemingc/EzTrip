# EZ-008 Planning Seed 确定性基线

EZ-008 冻结 10 条面向中国用户的结构化旅行请求，并用当前 `minimal-planning-graph-v1` 离线重放。它的用途是给后续 Constraint / Explore Agent 提供不变的输入、路由和来源基线，不是对推荐质量或完整行程质量的打分。

## 数据集覆盖

| Case | Tier | 主要覆盖 | Provider 行为 | 当前预期状态 |
| --- | --- | --- | --- | --- |
| 北京历史文化 | standard | 成人、预算、已确认必去、轻步行 | AMap capture fixture success | `candidates_ready` |
| 北京无预算 | standard | 可选澄清、预算能力单独阻塞 | AMap capture fixture success | `candidates_ready` |
| 北京无必去 | standard | 不做未经评测的开放式推荐 | provider forbidden | `no_candidate_query` |
| 上海外滩 | standard | 上海行政区代码、城市候选一致性 | labelled eval fixture success | `candidates_ready` |
| 成都亲子熊猫基地 | standard | 儿童、亲子偏好、成都行政区代码 | labelled eval fixture success | `candidates_ready` |
| 北京老人低预算 | standard | 老人、无障碍、轻步行、低预算保留 | AMap capture fixture success | `candidates_ready` |
| 未配置南京 | hard | provider 前阻断、V1 城市边界 | provider forbidden | `needs_clarification` |
| 酒店预算缺房间数 | hard | 整体需澄清但 POI 能力继续 | AMap capture fixture success | `candidates_ready` |
| 未确认必去故宫 | hard | Agent 推断不静默升级 | provider forbidden | `no_candidate_query` |
| provider 最终超时 | hard | typed failure、禁止伪造降级候选 | injected fixture failure | `provider_failed` |

`standard` 表示当前 Graph 的常规输入/继续路径，不代表未来预算、无障碍或排程一定容易；例如老人低预算 case 只验证约束被完整保留，预算可满足性列在 `future_expectations`，当前不会计入已通过能力。

## 当前基线结果

提交的 [`minimal-planning-graph-baseline.v1.json`](../../evals/reports/minimal-planning-graph-baseline.v1.json) 由 runner 机械生成：

- 10/10 cases 的当前预期通过；
- 120/120 个确定性检查通过；
- 6/6 个返回候选具有 provider、provider ID 和 fixture 数据模式；
- 数据集 SHA256：`e4bc2529b052beca15d31041ed5c8c2d88fe87b38bb4797f28ff8aff36c6d7cd`。

每条 case 固定检查：结果契约、workflow status、readiness、ready/blocked capability partition、三个约束桶、候选数量/名称、provider 调用参数和来源追溯。报告不使用 LLM judge，因此相同代码和数据集会得到相同结果。

这些比例不能表述成“行程准确率 100%”或“Agent 成功率 100%”。输入已经是 `TripRequest`，provider 是显式 fixture/scenario，当前 Graph 不做中文抽取、开放式推荐、排序、预算验证、天气/路线汇合或逐日排程。

## 资产与重放

- `evals/cases/planning-seed/manifest.json`：固定顺序、6 standard + 4 hard 库存；
- `evals/schemas/planning-seed-case.v1.json`：从 Pydantic case contract 机械导出；
- 每个 case：结构化请求、provider 行为、当前期望、未来期望和真实性边界；
- `evals/reports/minimal-planning-graph-baseline.v1.json`：可重现聚合报告。

```powershell
Set-Location backend
uv run python -m scripts.export_planning_seed_schema
uv run python -m scripts.run_planning_seed_eval
uv run python -m scripts.run_planning_seed_eval --write-report
uv run pytest tests/test_planning_seed_eval.py --no-cov
```

默认 runner 显式关闭 LangSmith 上传，也不会访问网络。EZ-101 已在同一批中文输入上增加 Constraint Agent 的独立期望标签和 live DeepSeek 报告，见 [`constraint-agent-baseline.md`](constraint-agent-baseline.md)；下一阶段建立单 Agent/单 Planner 纵向基线，达到 Gate 2 后才增加 Explore、Stay 等专业 Agent。
