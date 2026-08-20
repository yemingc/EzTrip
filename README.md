# EzTrip（易行）

面向中国城市自由行的可验证、可调整、可追溯旅行规划助手。

当前处于 Gate 0 工程基线阶段。仓库只包含可运行的 FastAPI/Next.js 空壳、PostgreSQL 迁移基线和 CI；尚未实现 Agent、地图检索或旅行规划能力，也没有可对外声称的质量指标。

## 技术基线

- Backend：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy、Alembic、uv
- Frontend：Next.js 16、React 19、TypeScript、Tailwind CSS、pnpm
- Infrastructure：PostgreSQL 17、Docker Compose、GitHub Actions

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

## 当前边界

- 不提供订票、订房、支付或实时房价；
- 当前健康页不是旅行规划产品完成度；
- 后续功能必须通过真实 provider contract、固定评测和可回放 trace 验证后再写入项目成果。
