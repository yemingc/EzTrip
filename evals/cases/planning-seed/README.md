# Planning seed cases V1

本目录包含 EZ-008 的 10 条可执行评测请求：6 条 `standard`、4 条 `hard`。`manifest.json` 冻结加载顺序；每个 case 同时包含有效 `TripRequest`、显式 provider scenario、当前 Graph 期望、未来节点期望和边界说明。

其中 `amap` fixture 候选来自已提交的北京 capture；上海和成都候选使用 `eval_fixture` 明确标记的合成场景。它们用于测试 Graph 业务语义，不得描述为实时高德搜索结果。

```powershell
Set-Location backend
uv run python -m scripts.run_planning_seed_eval
uv run pytest tests/test_planning_seed_eval.py --no-cov
```

新增或修改 case 后必须重新导出 schema、生成报告并提交两者；测试会检查 manifest 库存、JSON Schema、Pydantic 跨字段语义、数据集哈希和完整重放结果。
