from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.planning_tasks import router as planning_tasks_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(planning_tasks_router)
