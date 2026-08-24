from fastapi import APIRouter

from app.api.routes.destinations import router as destinations_router
from app.api.routes.health import router as health_router
from app.api.routes.maps import router as maps_router
from app.api.routes.planning_tasks import router as planning_tasks_router
from app.api.routes.request_intakes import router as request_intakes_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(destinations_router)
api_router.include_router(maps_router)
api_router.include_router(request_intakes_router)
api_router.include_router(planning_tasks_router)
