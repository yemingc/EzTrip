from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.tasks.contracts import (
    PlanningTaskAccepted,
    PlanningTaskCreateRequest,
    PlanningTaskEvent,
    PlanningTaskReviewDecisionAccepted,
    PlanningTaskReviewDecisionRequest,
    PlanningTaskSnapshot,
)
from app.tasks.service import (
    PlanningTaskNotFoundError,
    PlanningTaskReviewConflictError,
    PlanningTaskService,
)

router = APIRouter(prefix="/planning-tasks", tags=["planning-tasks"])


def get_planning_task_service(request: Request) -> PlanningTaskService:
    service = getattr(request.app.state, "planning_task_service", None)
    if not isinstance(service, PlanningTaskService):
        raise RuntimeError("planning task service is not configured")
    return service


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error_code": "planning-task-not-found",
            "message": f"规划任务 {task_id} 不存在。",
        },
    )


def _bad_cursor(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error_code": "invalid-event-cursor", "message": message},
    )


def _review_conflict(error: PlanningTaskReviewConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error_code": error.error_code, "message": error.message},
    )


@router.post("", response_model=PlanningTaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_planning_task(
    payload: PlanningTaskCreateRequest,
    request: Request,
) -> PlanningTaskAccepted:
    return await get_planning_task_service(request).submit(payload)


@router.get("/{task_id}", response_model=PlanningTaskSnapshot)
async def get_planning_task(task_id: str, request: Request) -> PlanningTaskSnapshot:
    try:
        return await get_planning_task_service(request).get(task_id)
    except PlanningTaskNotFoundError as error:
        raise _not_found(task_id) from error


@router.post(
    "/{task_id}/review-decisions",
    response_model=PlanningTaskReviewDecisionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_planning_task_review(
    task_id: str,
    payload: PlanningTaskReviewDecisionRequest,
    request: Request,
) -> PlanningTaskReviewDecisionAccepted:
    try:
        return await get_planning_task_service(request).submit_review(task_id, payload)
    except PlanningTaskNotFoundError as error:
        raise _not_found(task_id) from error
    except PlanningTaskReviewConflictError as error:
        raise _review_conflict(error) from error


def _encode_event(event: PlanningTaskEvent) -> str:
    return f"id: {event.event_id}\nevent: {event.kind.value}\ndata: {event.model_dump_json()}\n\n"


@router.get("/{task_id}/events", response_class=StreamingResponse)
async def stream_planning_task_events(
    task_id: str,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    service = get_planning_task_service(request)
    try:
        header_cursor = service.parse_event_cursor(task_id, last_event_id)
        if last_event_id is not None and after not in {0, header_cursor}:
            raise ValueError("after query and Last-Event-ID disagree")
        cursor = header_cursor if last_event_id is not None else after
        await service.events_after(task_id, cursor)
    except PlanningTaskNotFoundError as error:
        raise _not_found(task_id) from error
    except ValueError as error:
        raise _bad_cursor(str(error)) from error

    async def event_stream() -> AsyncIterator[str]:
        async for event in service.stream_events(task_id, after_sequence=cursor):
            if await request.is_disconnected():
                return
            yield ": heartbeat\n\n" if event is None else _encode_event(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
