from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status

from app.destinations import (
    DestinationResolutionConfigurationError,
    DestinationResolutionService,
)
from app.domain.base import DomainModel, NonEmptyText
from app.domain.destination import DestinationResolution
from app.domain.sources import DataMode
from app.providers.errors import ProviderRequestError

router = APIRouter(prefix="/destinations", tags=["destinations"])


class DestinationResolveRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    input_name: NonEmptyText
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE] = DataMode.FIXTURE


def _service(request: Request) -> DestinationResolutionService:
    service = getattr(request.app.state, "destination_resolution_service", None)
    if not isinstance(service, DestinationResolutionService):
        raise RuntimeError("destination resolution service is not configured")
    return service


@router.post("/resolve", response_model=DestinationResolution)
async def resolve_destination(
    payload: DestinationResolveRequest,
    request: Request,
) -> DestinationResolution:
    try:
        return await _service(request).resolve(payload.input_name, data_mode=payload.data_mode)
    except DestinationResolutionConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "destination-resolution-configuration",
                "message": "服务端尚未配置高德 Key, 无法使用实时城市解析。",
            },
        ) from error
    except ProviderRequestError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": (
                    f"destination-provider-{error.failure.category.value.replace('_', '-')}"
                ),
                "message": "城市解析服务暂时无法完成请求, 请稍后重试。",
                "retryable": error.failure.retryable,
            },
        ) from error
