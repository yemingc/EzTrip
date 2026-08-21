# EZ-101 Constraint Agent

EZ-101 是 EzTrip 第一个真实模型 Agent。它只处理已经通过 `TripRequest` 校验的 `raw_text`，把中文旅行约束提议成严格 schema；它不解析城市、日期、人数和预算，也不搜索景点或生成行程。

```mermaid
flowchart LR
    A[TripRequest.raw_text] --> B[DeepSeek propose_constraints]
    B -->|forced function tool| C[ConstraintProposalBatch]
    C --> D[deterministic normalizer]
    D --> E[ConstraintSet]
    E --> F[TripRequest revalidation]
    F --> G[PlannerContext / clarification]
```

## 模型与代码的边界

DeepSeek 只能提交：

- `kind`、`value`、`strength` 和 `priority`；
- 原文中的连续 `evidence`；
- `evidence_mode=explicit|inferred`。

工具 schema 不包含 `constraint_id`、`source` 或 `confirmed`。普通代码会：

- 验证 evidence 必须逐字存在于原文；
- 规范化 `walking_intensity` 为 `low|medium|high`；
- 拒绝重复语义、未知字段、非法值和冲突约束集合；
- 从规范值生成稳定哈希 ID；
- 把 `explicit` 映射为 `user_explicit + confirmed=true`；
- 把 `inferred` 固定映射为 `agent_inferred + confirmed=false`，进入 HITL；
- 在原文出现“可能、尚未确认、还没决定”等标记时，拒绝模型把提议升级为 explicit。

因此模型可以提出语义，但不能直接决定领域对象的来源、确认状态和身份。未确认硬约束仍由 `PlannerContext` 阻止最终定稿，不能静默触发 provider 查询。

## 运行方式

CI 使用 injected fake model，不读取 `.env`、不访问网络，也不上传 LangSmith：

```powershell
Set-Location backend
uv run pytest tests/test_constraint_agent.py tests/test_constraint_agent_evaluation.py --no-cov
```

真实评测必须显式确认网络调用，会使用本地 DeepSeek Key 并把 trace 上传到配置的 LangSmith project：

```powershell
uv run python -m scripts.run_constraint_agent_eval --live
```

报告只保存冻结 case 的规范标签、检查结果、token 和端到端延迟，不保存模型原始 tool arguments 或异常正文。Graph state 包含原始旅行文本；当前 live eval 输入都是仓库内的合成 case，未来产品接入 tracing 前仍需明确用户数据策略，不能把 metadata 不含 raw text 等同于完整 trace 不含 raw text。

## 当前限制

- 上游仍需先提供除 constraints 外的结构化 `TripRequest`；
- 当前 Agent 是独立两节点子图，尚未接到产品 planning Graph 或 FastAPI；
- 仅支持字符串约束值和 12 条以内的 V1 提议；
- exact evidence 与不确定性标记是保守防线，不是通用 prompt-injection 解决方案；
- strength 仍包含语义判断，老人“希望减少台阶”在 live baseline 中暴露了 hard/soft 歧义；
- Agent 不拥有 provider 工具，不能生成或伪造 POI、天气、路线、价格和酒店事实。
