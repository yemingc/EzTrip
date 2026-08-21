# EZ-101 Constraint Agent live 基线

本报告在 EZ-008 的同一组 6 standard + 4 hard 中文 `raw_text` 上运行 `deepseek-v4-pro`，比较版本化 Constraint Agent 期望标签与 schema 校验后的实际标签。它衡量约束抽取边界，不衡量行程质量。

提交的 point-in-time 报告是 [`deepseek-constraint-agent-baseline-2026-08-21.json`](../../evals/reports/deepseek-constraint-agent-baseline-2026-08-21.json)，Agent 数据集 SHA256 为 `7aafbb5a44463ce4d8c24fa01d66d987121b8a2dfef4bad4346da0effe96457e`。

## 最终结果

| 指标 | 结果 | 含义 |
| --- | ---: | --- |
| exact cases | 9/10 | 每条 case 的语义、source、confirmed 和澄清集合全部匹配 |
| semantic precision | 0.9375 | 16 条实际约束中 15 条语义匹配 |
| semantic recall | 0.9375 | 16 条期望约束中 15 条语义匹配 |
| confirmation accuracy | 1.0000 | 15 条语义匹配项的 source/confirmed 均正确 |
| clarification case rate | 1.0000 | 10/10 case 的待确认集合匹配 |
| usage coverage | 10/10 | 10 次调用都返回 token usage |
| total tokens | 10362 | 本次 10-case point-in-time 总量 |
| p50 / p95 end-to-end | 3166 / 4668 ms | 包含 Agent Graph、模型调用和每 case LangSmith flush |

precision/recall 的分母只有 16 条约束，1.0000 的 confirmation/clarification 也不能外推为生产正确率。token 和延迟是 2026-08-21 的一次实测，不是 SLA。

## 唯一失败 case

`seed-standard-beijing-senior-low-budget-v1` 的原文是“一位成年人陪两位老人……希望少走路、减少台阶”。期望标签把 `walking_intensity=low` 设为 soft、`accessibility=减少台阶` 设为 hard；模型正确抽取了三条约束和确认状态，但把 accessibility 判为 soft，因此该 case 不通过 exact match。

本实现保留这个失败：不为了 10/10 修改标签，也不加入“老人 + 台阶就强制 hard”的脆弱关键词补丁。后续更合理的方向是让 strength 歧义形成 typed clarification，或用更多老人/轮椅/无障碍 case 判断规则，而不是根据单条样本过拟合。

## 评测资产和复现

- `evals/cases/constraint-agent/expectations.v1.json`：独立于已有 `TripRequest.constraints` 的 Agent 标签；
- `evals/schemas/constraint-agent-expectations.v1.json`：期望标签 schema；
- `evals/schemas/constraint-agent-report.v1.json`：报告 schema；
- `evals/reports/deepseek-constraint-agent-baseline-2026-08-21.json`：提交的真实模型结果；
- `tests/test_constraint_agent_evaluation.py`：fixture evaluator、失败脱敏、schema 和报告聚合验证。

```powershell
Set-Location backend
uv run python -m scripts.export_constraint_agent_schemas
uv run pytest tests/test_constraint_agent.py tests/test_constraint_agent_evaluation.py --no-cov
uv run python -m scripts.run_constraint_agent_eval --live
```

前两条命令离线运行。最后一条会产生 DeepSeek 用量并上传 LangSmith trace，不能放入 CI。fixture 10/10 只证明 Agent 边界与 evaluator 可重放，真实模型效果必须引用上述 9/10 live 报告。
