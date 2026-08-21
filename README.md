# EzTrip（易行）

面向中国城市自由行的可验证、可调整、可追溯旅行规划助手。

当前已完成 Gate 0 工程基线、规格级 smoke scenarios、可观测性探针、第一版旅行领域契约、高德官方 MCP / REST 探针、typed provider adapter、脱敏 fixture、`TripRequest → PlannerContext` 确定性编译层、首条三节点 LangGraph 主链、6 standard + 4 hard 的确定性基线，以及首个 schema-constrained Constraint Agent 和真实 DeepSeek 评测。该 Agent 仍是独立子图；仓库尚未实现多 Agent 完整工作流、产品规划 API 或逐日行程生成。

## 技术基线

- Backend：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy、Alembic、uv
- Frontend：Next.js 16、React 19、TypeScript、Tailwind CSS、pnpm
- Infrastructure：PostgreSQL 17、Docker Compose、GitHub Actions
- Agent infrastructure：LangGraph、DeepSeek OpenAI-compatible API、LangSmith

## 本地启动

### 1. 环境配置

```powershell
Copy-Item .env.example .env
```

`.env` 仅用于本地，不能提交。当前模板预留 DeepSeek、LangSmith 和高德官方 MCP 配置；真实 Key 只填写在本地 `.env`。

### 2. PostgreSQL

```powershell
docker compose up -d postgres
docker compose ps
```

开发数据库默认映射到主机 `55432` 端口，以避开本机可能已有的 PostgreSQL `5432` 服务；容器内部仍使用标准 `5432`。

### 3. Backend

```powershell
Set-Location backend
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

健康检查：`GET http://localhost:8000/api/health`

### 4. Frontend

```powershell
Set-Location frontend
pnpm install
pnpm dev
```

页面：`http://localhost:3000`

## 验证

Backend：

```powershell
Set-Location backend
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

Frontend：

```powershell
Set-Location frontend
pnpm lint
pnpm typecheck
pnpm build
```

## Gate 0 smoke scenarios

`evals/cases/smoke/` 固化了 3 条规格级场景：正常北京三日游、确定性预算冲突、天气工具主动发现风险。它们当前只定义输入、注入条件和必须/禁止行为，不代表 Agent 或真实数据源已经实现。

```powershell
Set-Location backend
uv run pytest tests/test_smoke_cases.py --no-cov
```

## Gate 0 architecture and observability probe

公开架构决策位于 [`docs/adr/`](docs/adr/)。隔离的三节点探针用于验证 `DeepSeek LLM → weather fixture tool → DeepSeek LLM` 的 LangSmith trace 层级、metadata、错误记录和上传前脱敏；它不是旅行规划实现，也不会调用高德或真实天气数据。

探针会产生少量 DeepSeek API 用量并向配置的 LangSmith Cloud 项目上传固定合成输入。CI 只运行 fake model 和 fixture 测试，不读取真实 Key、调用模型或上传 trace。

```powershell
Set-Location backend
uv run python -m scripts.run_observability_probe
uv run python -m scripts.run_observability_probe --force-tool-error
```

## V1 domain contracts

[`docs/contracts/`](docs/contracts/) 保存从 Pydantic 模型机械导出的 JSON Schema bundle 和可解析示例，覆盖旅行请求、约束、候选 POI/住宿、天气风险、路线、费用、逐日计划、校验问题与计划版本。契约会拒绝未知字段和关键跨字段冲突，并明确区分 live、fixture、用户输入与估算数据。

这些对象目前是后续 Agent、provider normalizer、确定性校验器和 API 的共享数据边界，不表示对应工作流已经实现。

```powershell
Set-Location backend
uv run python -m scripts.export_domain_schemas
uv run pytest tests/test_domain_contract_examples.py tests/test_domain_models.py --no-cov
```

## PlannerContext 确定性编译

[`docs/planning/planner-context.md`](docs/planning/planner-context.md) 说明结构化 `TripRequest` 如何被机械编译成日期与住宿晚数、人数与间夜、预算参考尺度、约束作用域、澄清问题和逐项能力门禁。相同输入会得到相同哈希和结果；房间数、预算或未确认约束不会被模型静默猜测。

```powershell
Set-Location backend
uv run python -m scripts.export_planner_context_example
uv run pytest tests/test_planner_context.py tests/test_domain_contract_examples.py --no-cov
```

当前编译器不负责解析中文原话、调用模型、搜索景点或生成行程；它为下一阶段的 LangGraph 澄清路由与候选搜索提供稳定输入。

## 首条可执行 LangGraph 主链

[`docs/planning/minimal-planning-graph.md`](docs/planning/minimal-planning-graph.md) 实现 `compile_context → clarification_gate → candidate_search`。条件路由按具体能力决定是否继续：缺少预算不影响景点查询，未配置城市则在 provider 调用前停止。候选只来自已确认 `must_visit` 约束与高德 live/fixture adapter；provider 失败会保存为 typed failure，不伪造推荐。

```powershell
Set-Location backend
uv run python -m scripts.run_minimal_planning_graph
```

默认命令完全离线，并明确关闭 LangSmith 上传。当前只返回带来源的必去景点候选，不做开放式推荐、排序、天气/路线汇合、预算分配或逐日排程。

## 10-case 可执行基线

[`docs/evaluation/planning-seed-baseline.md`](docs/evaluation/planning-seed-baseline.md) 固化 6 条 standard 和 4 条 hard 中国旅行请求，覆盖北京/上海/成都、亲子、老人、低预算、缺预算、缺房间数、未确认约束、未配置城市和 provider timeout。

```powershell
Set-Location backend
uv run python -m scripts.run_planning_seed_eval
```

当前机械报告为 10/10 cases、120/120 deterministic checks、6/6 candidate sources traceable。输入已经结构化且数据来自明确 fixture/scenario，因此这些数字不能写成行程准确率或 Agent 成功率；它们是后续增加 Agent 前的可重放基线。

## 首个 schema-constrained Agent

[`docs/agents/constraint-agent.md`](docs/agents/constraint-agent.md) 实现独立的 `propose_constraints → deterministic_normalizer` Agent 子图。DeepSeek 只提交原文证据和约束语义，不能直接设置 ID、source 或 confirmed；推断约束固定保持未确认并进入 HITL。

[`docs/evaluation/constraint-agent-baseline.md`](docs/evaluation/constraint-agent-baseline.md) 记录同一 10-case 上的真实 `deepseek-v4-pro` point-in-time 结果：9/10 exact cases，semantic precision/recall 均为 0.9375，confirmation accuracy 与 clarification case rate 均为 1.0000，共 10362 tokens，端到端 p50/p95 为 3166/4668 ms。唯一失败是老人“希望减少台阶”的 hard/soft 歧义；该失败被保留，没有为追求满分修改标签。

```powershell
Set-Location backend
uv run pytest tests/test_constraint_agent.py tests/test_constraint_agent_evaluation.py --no-cov
uv run python -m scripts.run_constraint_agent_eval --live
```

测试默认使用 fake model。`--live` 会产生 DeepSeek 用量并上传 LangSmith trace，不能在 CI 运行。当前 Agent 只替换已有 `TripRequest` 的 constraints slice，尚未接入产品 API、主 planning Graph 或完整行程生成。

## AMap live contract probe

[`docs/probes/amap-mcp-live-probe-2026-08-20.md`](docs/probes/amap-mcp-live-probe-2026-08-20.md) 记录一次固定北京真实探针：官方 MCP 当前发现 15 个工具，并验证 POI、天气、距离、步行和公交响应；版本化 fixture 只保留字段白名单并执行凭据/PII 脱敏。CI 回放 fixture，不读取 Key 或访问高德。

真实探针会消耗少量高德配额，必须在本地配置 Key 并显式确认：

```powershell
Set-Location backend
uv run python -m scripts.run_amap_mcp_probe --live --write-fixture
```

这只证明 provider 字段可用性和已知边界，不表示 Agent 或地图搜索产品功能已经实现。

## AMap typed provider adapter

[`docs/providers/amap-provider-contract.md`](docs/providers/amap-provider-contract.md) 说明业务层如何通过同一 port 使用 live MCP/REST 与离线 fixture。adapter 当前把高德结果归一化成 `CandidatePOI`、`WeatherRisk` 和 `RouteLeg`，并统一处理来源时间、响应哈希、字段漂移、超时、限流与认证失败。

默认 fixture smoke 不联网：

```powershell
Set-Location backend
uv run python -m scripts.run_amap_provider_smoke
```

显式 `--live` 才会读取本地 Key 并消耗少量高德配额：

```powershell
uv run python -m scripts.run_amap_provider_smoke --live
```

天气风险由 provider 数据触发并通过确定性阈值生成，不需要用户先追加“第二天下雨”。当前仍没有 Agent 使用这些结果生成行程。

## 当前边界

- 不提供订票、订房、支付或实时房价；
- 当前健康页不是旅行规划产品完成度；
- 当前三节点探针只证明观测链路可接入，不证明模型规划质量；
- 当前领域契约、PlannerContext 编译器和 provider adapter 已接入首条确定性 LangGraph；首个 Constraint Agent 已形成独立子图和 live 基线，但尚未接入产品主链，多 Agent 协作仍未实现；
- 高德 live fixture 是 2026-08-20 的点时样本，不是当前天气、实时酒店价格或生产 SLA；
- 后续功能必须通过真实 provider contract、固定评测和可回放 trace 验证后再写入项目成果。
