from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.tasks.executor import ProductGraphPlanningTaskExecutor
from app.tasks.service import PlanningTaskService


def create_app(
    *,
    settings: Settings | None = None,
    planning_task_service: PlanningTaskService | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.state.planning_task_service = planning_task_service or PlanningTaskService(
        ProductGraphPlanningTaskExecutor(resolved_settings),
        heartbeat_seconds=resolved_settings.planning_sse_heartbeat_seconds,
        timeout_seconds=resolved_settings.planning_task_timeout_seconds,
    )
    application.include_router(api_router, prefix="/api")
    return application


app = create_app()
