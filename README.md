# EzTrip（易行）

面向中国城市自由行的可验证旅行规划助手。EzTrip 将 Provider 数据、LLM 规划、确定性约束、局部修复和人工确认组合成一条可追踪、可恢复的规划链路。

> 这是 AI 应用工程作品集，不是订票、订房或支付平台；预算为规划参考，不是实时价格或交易报价。

在线体验：[EzTrip 行程演示](https://yemingc.github.io/EzTrip/)。演示使用已冻结的数据快照，不调用实时模型或第三方服务。

## 核心流程

```text
需求理解与确认
  → Explore / Stay / Weather 并行检索
  → 路线与预算材料
  → Plan Agent
  → Hard Validator
  → 有界 Repair Router
  → 人工审核与 PlanVersion
```

| 能力 | 实现 |
| --- | --- |
| 需求确认 | 中文 Request Intake 提取带原文 evidence 的字段与约束；日期、人数、预算等由确定性代码复算，确认后才进入规划 |
| Provider-grounded 规划 | Explore、Stay 和 Weather 并行；地点、路线与天气来自高德 adapter 或明确标记的离线 fixture |
| 安全定稿 | Pydantic 契约、Hard Validator 和有界 Repair Router 阻止模型静默补造来源、价格或营业时间 |
| 人机协作 | 规划在 HITL 边界暂停，支持批准、冲突确认和结构化局部修改 |
| 恢复与追踪 | SQLite task ledger、LangGraph checkpoint、可回放 SSE、PlanVersion 和 LangSmith trace |
| 预算估算 | 根据住宿晚数、排程景点、路线、人数和天数确定性聚合版本化参考区间；不由 LLM 生成精确金额 |

产品主链为 `run_specialists → build_materials → run_plan_agent → validate_hard_plan → [run_repair] → HITL`。架构细节见 [planning task API](docs/api/planning-task-api.md) 和 [Product Graph 相关文档](docs/planning/specialist-fanout.md)。

## 技术栈

- Backend：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy、LangGraph、uv
- Frontend：Next.js 16、React 19、TypeScript、Tailwind CSS、Playwright、pnpm
- Providers：DeepSeek、LangSmith、高德 MCP / REST；默认测试使用离线 fixture
- Infrastructure：SQLite task/checkpoint store、PostgreSQL 17、Docker Compose、GitHub Actions

## 本地启动

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22、pnpm 11 和 Docker。

### 1. 配置环境与数据库

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
docker compose ps
```

`.env` 只用于本地且不能提交。默认 PostgreSQL 映射到 `localhost:55432`；fixture 模式不需要 DeepSeek、LangSmith 或高德 Key。

### 2. 启动 Backend

```powershell
Set-Location backend
uv sync --locked --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

- Health：<http://localhost:8000/api/health>
- OpenAPI：<http://localhost:8000/docs>

### 3. 启动 Frontend

在另一个终端中运行：

```powershell
Set-Location frontend
pnpm install --frozen-lockfile
pnpm dev
```

打开 <http://localhost:3000>。选择 `live` 即授权本次请求调用 DeepSeek 与高德并消耗配额；Key 始终保留在服务端。

## 验证

Backend：

```powershell
Set-Location backend
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy app scripts
uv run pip-audit
uv run pytest
```

Frontend：

```powershell
Set-Location frontend
pnpm audit --audit-level high
pnpm lint
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

`test:e2e` 会启动 fixture backend 和 frontend，不读取 API Key，也不调用 DeepSeek 或实时高德。

以下是有提交和报告对应的时间点证据，不代表实时 Provider 质量或生产 SLA：

| 证据 | 结果与边界 |
| --- | --- |
| [Budget V2 发布门禁](docs/release-readiness.md) | 2026-08-27，`fa1f0e2`：Backend 453/453、app branch coverage 85%、Playwright 22/22、Python/Node 审计 0 个已知漏洞；另有用户浏览器验收和 PR CI |
| [30-case fixture 对照](docs/evaluation/system-comparison-protocol.md) | 完整 Product Graph 在设计故障集上 finalizable 21/29，两条对照路径均为 5/29；只证明 Validator + Repair 控制路径的恢复价值 |
| [Repeated-live pilot](docs/evaluation/live-comparison-pilot-result.md) | 三个 arm 都为 6/6 finalizable，未观察到成功率提升；Product 重复一致性更高，但模型调用和累计延迟也更高 |
| [Live browser canary](docs/evaluation/live-browser-canary-2026-08-25.md) | 一次泉州 2 日点时链路完成并跨刷新恢复；同轮也记录 Provider 空结果降级，不可外推到全国城市质量 |

完整发布复现、配置加固和已知限制见 [Release Readiness](docs/release-readiness.md)。

## 文档导航

- [API 与任务状态协议](docs/api/planning-task-api.md)
- [领域契约与 JSON Schema](docs/contracts/README.md)
- [架构决策 ADR](docs/adr/README.md)
- [PlannerContext 确定性编译](docs/planning/planner-context.md)
- [Specialist fan-out](docs/planning/specialist-fanout.md)
- [路线与预算材料层](docs/planning/route-budget-materials.md)
- [Plan Agent](docs/agents/plan-agent.md)
- [Hard Validators](docs/planning/hard-validators.md)
- [Repair Router](docs/planning/repair-router.md)
- [Checkpoint 与 HITL](docs/planning/stateful-checkpoint-hitl.md)
- [高德 Provider contract](docs/providers/amap-provider-contract.md)
- [评测与 fixtures](evals/README.md)
- [前端交互边界](frontend/README.md)

## 当前边界

- 不提供预订、支付、实时房价、票价、房态或生产 SLA。
- 北京、上海、成都 fixture 只用于确定性回归，不代表 live 城市白名单或全国质量。
- live 模式依赖 DeepSeek 与高德；缺失、超时和证据不足会显式降级或阻止定稿。
- SQLite task ledger 与 checkpoint 面向本地单实例，不具备多 worker 协调、账号隔离、加密、高可用或 exactly-once 外部副作用保证。
- 确认前的 Request Intake 草案仍在单进程内存；创建任务后才支持 URL、snapshot、SSE 与审核状态恢复。
- Weather Repair Coordinator 已有隔离验证，但尚未接入产品主图；开放式自然语言重规划也不在当前范围内。
