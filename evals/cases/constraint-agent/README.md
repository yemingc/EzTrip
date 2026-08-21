# Constraint Agent expectations V1

`expectations.v1.json` 复用 EZ-008 的 10 条中文 `raw_text`，但单独冻结 Constraint Agent 的语义标签。这样 `travel_styles` 与旧 `TripRequest.constraints` 的字段分工不会反向污染模型评测标签。

标签只包含 `kind`、规范值、`strength`、`source` 和 `confirmed`；evaluator 另外验证模型 evidence 必须来自原文，但报告不保存 evidence 或原始 tool arguments。

修改 planning seed、Agent 期望、Prompt 或 normalizer 后，必须：

```powershell
Set-Location backend
uv run python -m scripts.export_constraint_agent_schemas
uv run pytest tests/test_constraint_agent.py tests/test_constraint_agent_evaluation.py --no-cov
uv run python -m scripts.run_constraint_agent_eval --live
```

最后一条命令会产生真实模型费用与 LangSmith trace；只有明确刷新 point-in-time live 报告时才运行。
