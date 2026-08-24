from fastapi import APIRouter, HTTPException, Request, status

from app.request_intake import (
    ConfirmedRequestIntake,
    RequestConfirmationDraft,
    RequestIntakeConfigurationError,
    RequestIntakeConfirmationError,
    RequestIntakeConfirmRequest,
    RequestIntakeCreateRequest,
    RequestIntakeNotFoundError,
    RequestIntakeProtocolError,
    RequestIntakeService,
)

router = APIRouter(prefix="/request-intakes", tags=["request-intakes"])


def _service(request: Request) -> RequestIntakeService:
    service = getattr(request.app.state, "request_intake_service", None)
    if not isinstance(service, RequestIntakeService):
        raise RuntimeError("request intake service is not configured")
    return service


@router.post("", response_model=RequestConfirmationDraft)
async def propose_request_intake(
    payload: RequestIntakeCreateRequest,
    request: Request,
) -> RequestConfirmationDraft:
    try:
        return await _service(request).propose(payload)
    except RequestIntakeConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "request-intake-configuration",
                "message": "服务端尚未配置实时需求理解所需的 DeepSeek/LangSmith。",
            },
        ) from error
    except RequestIntakeProtocolError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "request-intake-protocol",
                "message": "需求理解结果未通过字段或 evidence 校验, 请重试或保留表单。",
            },
        ) from error


@router.post("/{draft_id}/confirm", response_model=ConfirmedRequestIntake)
async def confirm_request_intake(
    draft_id: str,
    payload: RequestIntakeConfirmRequest,
    request: Request,
) -> ConfirmedRequestIntake:
    try:
        return await _service(request).confirm(draft_id, payload)
    except RequestIntakeNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "request-intake-not-found",
                "message": "需求确认草案不存在或服务已重启, 请重新理解需求。",
            },
        ) from error
    except RequestIntakeConfirmationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "request-intake-invalid-confirmation", "message": str(error)},
        ) from error
