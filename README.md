# EzTrip（易行）

面向中国城市自由行的可验证、可调整、可追溯旅行规划助手。

当前已完成 Gate 0 工程基线、规格级 smoke scenarios、可观测性探针，以及第一版旅行领域契约。仓库尚未实现生产旅行 Agent、地图检索或可执行旅行规划，也没有可对外声称的规划质量指标。

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

## 当前边界

- 不提供订票、订房、支付或实时房价；
- 当前健康页不是旅行规划产品完成度；
- 当前三节点探针只证明观测链路可接入，不证明模型规划质量；
- 当前领域契约只证明数据结构和校验边界，不证明 provider 或多 Agent 已接入；
- 后续功能必须通过真实 provider contract、固定评测和可回放 trace 验证后再写入项目成果。
