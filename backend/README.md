# EzTrip API

Gate 0 FastAPI service. It currently exposes only a liveness-style health endpoint and an empty Alembic baseline.

```powershell
uv sync --all-groups
uv run uvicorn app.main:app --reload
```
