import asyncio
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from pydantic import ValidationError

from app.agents.constraint_agent import (
    ConstraintAgentConfigurationError,
    run_constraint_agent_for_text,
    run_live_constraint_agent_for_text,
)
from app.agents.contracts import ConstraintAgentResult
from app.core.config import Settings
from app.domain.money import BudgetCategory
from app.domain.request import (
    BudgetConstraint,
    Constraint,
    ConstraintSet,
    ConstraintSource,
    Party,
    TripRequest,
)
from app.domain.sources import DataMode
from app.request_intake.agent import (
    RequestIntakeConfigurationError,
    RequestIntakeProtocolError,
    run_live_request_intake_agent,
    run_request_intake_agent,
)
from app.request_intake.contracts import (
    ConfirmedRequestIntake,
    ProposedRequestFields,
    RequestConfirmationDraft,
    RequestFieldDecision,
    RequestFieldDecisionStatus,
    RequestFieldName,
    RequestIntakeConfirmRequest,
    RequestIntakeCreateRequest,
    RequestIntakeFormValues,
    RequestIntakeSelection,
)
from app.request_intake.fixtures import (
    FixtureConstraintProposalModel,
    FixtureRequestFieldProposalModel,
)


class RequestIntakeNotFoundError(KeyError):
    pass


class RequestIntakeConfirmationError(RuntimeError):
    pass


@dataclass(frozen=True)
class _DraftRecord:
    payload: RequestIntakeCreateRequest
    draft: RequestConfirmationDraft


class RequestIntakeService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._drafts: dict[str, _DraftRecord] = {}
        self._lock = asyncio.Lock()

    async def propose(self, payload: RequestIntakeCreateRequest) -> RequestConfirmationDraft:
        draft_id = f"request-intake-{uuid4().hex}"
        if payload.data_mode == DataMode.FIXTURE:
            field_result, constraint_result = await asyncio.gather(
                asyncio.to_thread(
                    run_request_intake_agent,
                    draft_id,
                    payload.raw_text,
                    payload.reference_date,
                    FixtureRequestFieldProposalModel(),
                ),
                asyncio.to_thread(
                    self._run_fixture_constraints,
                    draft_id,
                    payload.raw_text,
                ),
            )
            model_call_count = 0
        else:
            try:
                field_result, constraint_result = await asyncio.gather(
                    asyncio.to_thread(
                        run_live_request_intake_agent,
                        draft_id,
                        payload.raw_text,
                        payload.reference_date,
                        self._settings,
                    ),
                    asyncio.to_thread(
                        self._run_live_constraints,
                        draft_id,
                        payload.raw_text,
                    ),
                )
            except RequestIntakeConfigurationError:
                raise
            except Exception as error:
                raise RequestIntakeProtocolError("live request intake failed") from error
            model_call_count = 2
        draft = self._assemble_draft(
            draft_id,
            payload,
            field_result.decisions,
            field_result.proposed_fields,
            field_result.clarifications,
            field_result.model,
            constraint_result,
            model_call_count,
        )
        async with self._lock:
            self._drafts[draft_id] = _DraftRecord(payload=payload, draft=draft)
        return draft

    async def confirm(
        self,
        draft_id: str,
        confirmation: RequestIntakeConfirmRequest,
    ) -> ConfirmedRequestIntake:
        async with self._lock:
            record = self._drafts.get(draft_id)
        if record is None:
            raise RequestIntakeNotFoundError(draft_id)
        if confirmation.selection == RequestIntakeSelection.PROPOSAL:
            if not record.draft.proposal_can_confirm:
                raise RequestIntakeConfirmationError(
                    "当前提议无法组成有效请求, 请选择保留结构化表单。"
                )
            fields = record.draft.proposed_fields
            constraints = self._confirm_constraints(record.draft.proposed_constraints)
            styles = fields.travel_styles
        else:
            fields = ProposedRequestFields()
            constraints = ConstraintSet()
            styles = ()
        request = self._build_trip_request(
            record.payload,
            fields,
            constraints,
            styles,
            selected_destination_adcode=confirmation.selected_destination_adcode,
        )
        return ConfirmedRequestIntake(
            confirmation_id=f"request-confirmation-{uuid4().hex}",
            draft_id=draft_id,
            selection=confirmation.selection,
            data_mode=record.payload.data_mode,
            selected_destination_adcode=confirmation.selected_destination_adcode,
            request=request,
        )

    def _run_fixture_constraints(self, request_id: str, raw_text: str) -> ConstraintAgentResult:
        return run_constraint_agent_for_text(
            request_id,
            raw_text,
            FixtureConstraintProposalModel(),
        )

    def _run_live_constraints(self, request_id: str, raw_text: str) -> ConstraintAgentResult:
        try:
            return run_live_constraint_agent_for_text(
                request_id,
                raw_text,
                self._settings,
            )
        except ConstraintAgentConfigurationError as error:
            raise RequestIntakeConfigurationError(str(error)) from error
        except Exception as error:
            raise RequestIntakeProtocolError("live constraint intake failed") from error

    def _assemble_draft(
        self,
        draft_id: str,
        payload: RequestIntakeCreateRequest,
        decisions: tuple[RequestFieldDecision, ...],
        proposed_fields: ProposedRequestFields,
        field_clarifications: tuple[str, ...],
        field_model: str,
        constraint_result: ConstraintAgentResult,
        model_call_count: int,
    ) -> RequestConfirmationDraft:
        assembled: list[RequestFieldDecision] = []
        proposed_names = {decision.field for decision in decisions}
        for decision in decisions:
            if decision.status == RequestFieldDecisionStatus.NEEDS_CONFIRMATION:
                assembled.append(
                    decision.model_copy(
                        update={"form_value": self._form_value(payload.form, decision.field)}
                    )
                )
                continue
            form_value = self._form_value(payload.form, decision.field)
            status = (
                RequestFieldDecisionStatus.PROPOSED
                if form_value is None
                else RequestFieldDecisionStatus.MATCHED
                if form_value == decision.proposed_value
                else RequestFieldDecisionStatus.CONFLICT
            )
            message = {
                RequestFieldDecisionStatus.PROPOSED: "原文提出了表单没有的值。",
                RequestFieldDecisionStatus.MATCHED: "原文提议与结构化表单一致。",
                RequestFieldDecisionStatus.CONFLICT: "原文提议与结构化表单不同, 请确认。",
            }[status]
            assembled.append(
                decision.model_copy(
                    update={"status": status, "form_value": form_value, "message": message}
                )
            )
        for field in (
            RequestFieldName.ORIGIN_CITY,
            RequestFieldName.DESTINATION_CITY,
            RequestFieldName.START_DATE,
            RequestFieldName.TRIP_DAYS,
            RequestFieldName.ADULTS,
            RequestFieldName.CHILDREN,
            RequestFieldName.SENIORS,
            RequestFieldName.BUDGET_LIMIT,
            RequestFieldName.PACE,
        ):
            if field not in proposed_names:
                assembled.append(
                    RequestFieldDecision(
                        field=field,
                        status=RequestFieldDecisionStatus.UNMENTIONED,
                        form_value=self._form_value(payload.form, field),
                        message="原文未提及, 将沿用结构化表单值。",
                    )
                )
        clarifications = list(field_clarifications)
        clarifications.extend(
            f"{decision.field.value} 与表单不同, 请确认。"
            for decision in assembled
            if decision.status == RequestFieldDecisionStatus.CONFLICT
        )
        clarifications.extend(
            "存在模型推断的约束, 请在确认卡中核对。"
            for decision in constraint_result.decisions
            if not decision.constraint.confirmed
        )
        proposal_can_confirm = True
        try:
            self._build_trip_request(
                payload,
                proposed_fields,
                self._confirm_constraints(constraint_result.constraints),
                proposed_fields.travel_styles,
                selected_destination_adcode=None,
            )
        except (ValidationError, RequestIntakeConfirmationError):
            proposal_can_confirm = False
            clarifications.append("原文提议组合后不满足 V1 请求契约, 请保留表单并修改。")
        return RequestConfirmationDraft(
            draft_id=draft_id,
            data_mode=payload.data_mode,
            raw_text_sha256=hashlib.sha256(payload.raw_text.encode("utf-8")).hexdigest(),
            field_model=field_model,
            constraint_model=constraint_result.model,
            model_call_count=model_call_count,
            field_decisions=tuple(assembled),
            proposed_fields=proposed_fields,
            constraint_decisions=constraint_result.decisions,
            proposed_constraints=constraint_result.constraints,
            clarifications=tuple(dict.fromkeys(clarifications)),
            proposal_can_confirm=proposal_can_confirm,
        )

    @staticmethod
    def _form_value(form: RequestIntakeFormValues, field: RequestFieldName) -> str | None:
        values: dict[RequestFieldName, object | None] = {
            RequestFieldName.ORIGIN_CITY: form.origin_city,
            RequestFieldName.DESTINATION_CITY: form.destination_city,
            RequestFieldName.START_DATE: form.start_date,
            RequestFieldName.TRIP_DAYS: form.trip_days,
            RequestFieldName.ADULTS: form.adults,
            RequestFieldName.CHILDREN: form.children,
            RequestFieldName.SENIORS: form.seniors,
            RequestFieldName.BUDGET_LIMIT: form.budget_limit,
            RequestFieldName.PACE: form.pace.value,
            RequestFieldName.TRAVEL_STYLE: None,
        }
        value = values[field]
        if value is None:
            return None
        if isinstance(value, Decimal):
            return format(value, "f")
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value)

    @staticmethod
    def _confirm_constraints(constraints: ConstraintSet) -> ConstraintSet:
        return ConstraintSet(
            items=tuple(
                Constraint(
                    **constraint.model_dump(
                        mode="python",
                        exclude={"source", "confirmed"},
                    ),
                    source=ConstraintSource.USER_CONFIRMED,
                    confirmed=True,
                )
                for constraint in constraints.items
            )
        )

    @staticmethod
    def _build_trip_request(
        payload: RequestIntakeCreateRequest,
        proposed: ProposedRequestFields,
        constraints: ConstraintSet,
        styles: tuple[str, ...],
        *,
        selected_destination_adcode: str | None,
    ) -> TripRequest:
        form = payload.form
        start_date = proposed.start_date or form.start_date
        trip_days = proposed.trip_days or form.trip_days
        adults = proposed.adults if proposed.adults is not None else form.adults
        children = proposed.children if proposed.children is not None else form.children
        seniors = proposed.seniors if proposed.seniors is not None else form.seniors
        budget_limit = proposed.budget_limit or form.budget_limit
        try:
            return TripRequest(
                request_id=f"web-request-{uuid4().hex}",
                raw_text=payload.raw_text,
                origin_city=proposed.origin_city or form.origin_city,
                destination_city=proposed.destination_city or form.destination_city,
                destination_adcode=selected_destination_adcode,
                start_date=start_date,
                end_date=start_date + timedelta(days=trip_days - 1),
                party=Party(
                    adults=adults,
                    children=children,
                    seniors=seniors,
                    rooms=form.rooms,
                ),
                budget=BudgetConstraint(
                    total_limit=budget_limit,
                    included_categories=(
                        BudgetCategory.LODGING,
                        BudgetCategory.TRANSPORT,
                        BudgetCategory.FOOD,
                        BudgetCategory.ADMISSION,
                        BudgetCategory.ACTIVITY,
                    ),
                    hard_limit=False,
                ),
                pace=proposed.pace or form.pace,
                travel_styles=styles,
                constraints=constraints,
            )
        except ValidationError as error:
            raise RequestIntakeConfirmationError(
                "确认后的字段无法组成有效的 2-5 天单城市请求。"
            ) from error
