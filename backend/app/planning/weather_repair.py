import hashlib
import json
import unicodedata
from collections.abc import Iterable
from datetime import date
from typing import Protocol

from app.domain.candidates import ActivityEnvironment, CandidatePOI
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, ItineraryItem, PlanStatus, TripPlan
from app.domain.request import TripRequest
from app.domain.travel_data import RiskSeverity, WeatherRisk
from app.domain.validation import PlanValidationReport
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.repair_contracts import RepairPlanDiff
from app.planning.weather_repair_contracts import (
    WeatherChangeGrade,
    WeatherImpact,
    WeatherPlanChange,
    WeatherRepairAttemptTrace,
    WeatherRepairOutcome,
    WeatherRepairResult,
    WeatherRepairStopReason,
    WeatherRepairTask,
    WeatherReplanExecutionResult,
    WeatherReplanExecutionStatus,
)

WEATHER_REPAIR_VERSION = "weather-repair-v1"
MAX_WEATHER_REPLAN_ATTEMPTS = 2
SIGNIFICANT_SEVERITIES = frozenset({RiskSeverity.MEDIUM, RiskSeverity.HIGH, RiskSeverity.EXTREME})


class WeatherReplanExecutor(Protocol):
    async def replan(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
        task: WeatherRepairTask,
        impacts: tuple[WeatherImpact, ...],
        attempt_index: int,
    ) -> WeatherReplanExecutionResult: ...


class HardPlanValidator(Protocol):
    def __call__(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
    ) -> PlanValidationReport: ...


def _canonical(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith(("P", "Z"))
    )


def _activity_tokens(candidate: CandidatePOI) -> frozenset[str]:
    tokens = {
        _canonical(candidate.environment.value),
        *(_canonical(value) for value in candidate.categories),
        *(_canonical(value) for value in candidate.tags),
    }
    if candidate.environment in {ActivityEnvironment.OUTDOOR, ActivityEnvironment.MIXED}:
        tokens.update({_canonical("outdoor"), _canonical("户外"), _canonical("室外")})
    if candidate.environment == ActivityEnvironment.INDOOR:
        tokens.update({_canonical("indoor"), _canonical("室内")})
    return frozenset(tokens)


def _matched_activity_types(
    candidate: CandidatePOI,
    risk: WeatherRisk,
) -> tuple[str, ...]:
    candidate_tokens = _activity_tokens(candidate)
    return tuple(
        activity_type
        for activity_type in risk.affected_activity_types
        if _canonical(activity_type) in candidate_tokens
    )


def detect_weather_impacts(
    plan: TripPlan,
    materials: PlanningMaterialBundle,
    risks: Iterable[WeatherRisk],
) -> tuple[WeatherImpact, ...]:
    """Deterministically match significant provider risks to exposed itinerary items."""

    candidates = {item.candidate_id: item for item in materials.shortlist.poi_candidates}
    impacts: list[WeatherImpact] = []
    for risk in risks:
        if risk.severity not in SIGNIFICANT_SEVERITIES or risk.city != plan.destination_city:
            continue
        for day in plan.days:
            for item in day.items:
                if (
                    item.kind not in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
                    or item.candidate_id is None
                    or item.candidate_id not in candidates
                    or risk.starts_at >= item.end_at
                    or item.start_at >= risk.ends_at
                ):
                    continue
                candidate = candidates[item.candidate_id]
                matched = _matched_activity_types(candidate, risk)
                if not matched:
                    continue
                impacts.append(
                    WeatherImpact(
                        risk_id=risk.risk_id,
                        item_id=item.item_id,
                        candidate_id=item.candidate_id,
                        service_date=day.date,
                        environment=candidate.environment,
                        severity=risk.severity,
                        matched_activity_types=matched,
                        risk_source=risk.source,
                    )
                )
    return tuple(
        sorted(
            impacts,
            key=lambda item: (item.service_date, item.item_id, item.risk_id),
        )
    )


def _candidate_ids(plan: TripPlan) -> tuple[str, ...]:
    return tuple(
        item.candidate_id
        for day in plan.days
        for item in day.items
        if item.candidate_id is not None
    )


def _plan_diff(before: TripPlan, after: TripPlan) -> RepairPlanDiff:
    before_days = {item.date: item for item in before.days}
    after_days = {item.date: item for item in after.days}
    before_candidates = set(_candidate_ids(before))
    after_candidates = set(_candidate_ids(after))
    return RepairPlanDiff(
        changed_dates=tuple(
            day
            for day in sorted(set(before_days) | set(after_days))
            if before_days.get(day) != after_days.get(day)
        ),
        added_candidate_ids=tuple(sorted(after_candidates - before_candidates)),
        removed_candidate_ids=tuple(sorted(before_candidates - after_candidates)),
        total_cost_minimum_before=before.total_cost_minimum,
        total_cost_minimum_after=after.total_cost_minimum,
        total_cost_maximum_before=before.total_cost_maximum,
        total_cost_maximum_after=after.total_cost_maximum,
    )


def _items_by_id(plan: TripPlan) -> dict[str, tuple[date, ItineraryItem]]:
    return {item.item_id: (day.date, item) for day in plan.days for item in day.items}


def _candidate_dates(plan: TripPlan) -> dict[str, date]:
    return {
        item.candidate_id: day.date
        for day in plan.days
        for item in day.items
        if item.candidate_id is not None
    }


def grade_weather_change(before: TripPlan, after: TripPlan) -> WeatherPlanChange:
    diff = _plan_diff(before, after)
    before_items = _items_by_id(before)
    after_items = _items_by_id(after)
    changed_item_ids = tuple(
        sorted(
            item_id
            for item_id in set(before_items) | set(after_items)
            if before_items.get(item_id) != after_items.get(item_id)
        )
    )
    before_dates = _candidate_dates(before)
    after_dates = _candidate_dates(after)
    cross_day = tuple(
        sorted(
            candidate_id
            for candidate_id in set(before_dates) & set(after_dates)
            if before_dates[candidate_id] != after_dates[candidate_id]
        )
    )
    reasons: list[str] = []
    if diff.added_candidate_ids or diff.removed_candidate_ids:
        reasons.append("candidate_set_changed")
    if cross_day:
        reasons.append("cross_day_move")
    if len(diff.changed_dates) > 1:
        reasons.append("multiple_dates_changed")
    grade = WeatherChangeGrade.NONE
    if changed_item_ids or diff.changed_dates:
        grade = WeatherChangeGrade.MAJOR if reasons else WeatherChangeGrade.MINOR
    return WeatherPlanChange(
        grade=grade,
        diff=diff,
        changed_item_ids=changed_item_ids,
        cross_day_candidate_ids=cross_day,
        major_reasons=tuple(reasons),
    )


def _task(
    request: TripRequest,
    plan: TripPlan,
    impacts: tuple[WeatherImpact, ...],
    max_attempts: int,
) -> WeatherRepairTask:
    affected_dates = tuple(sorted({item.service_date for item in impacts}))
    risk_ids = tuple(sorted({item.risk_id for item in impacts}))
    impacted_item_ids = tuple(sorted({item.item_id for item in impacts}))
    material = json.dumps(
        {
            "request_id": request.request_id,
            "plan_id": plan.plan_id,
            "risk_ids": risk_ids,
            "item_ids": impacted_item_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return WeatherRepairTask(
        task_id=f"weather-repair-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}",
        request_id=request.request_id,
        affected_dates=affected_dates,
        protected_dates=tuple(day.date for day in plan.days if day.date not in affected_dates),
        risk_ids=risk_ids,
        impacted_item_ids=impacted_item_ids,
        max_attempts=max_attempts,
    )


def _scope_is_valid(
    before: TripPlan,
    after: TripPlan,
    impacts: tuple[WeatherImpact, ...],
) -> bool:
    if (
        before.request_id != after.request_id
        or before.plan_id != after.plan_id
        or before.status != after.status
        or before.destination_city != after.destination_city
        or before.start_date != after.start_date
        or before.end_date != after.end_date
        or before.cost_items != after.cost_items
        or before.weather_risks != after.weather_risks
    ):
        return False
    impacted_item_ids = {item.item_id for item in impacts}
    impacted_candidate_ids = {item.candidate_id for item in impacts}
    before_items = _items_by_id(before)
    after_items = _items_by_id(after)
    if any(
        item_id not in after_items
        or before_items[item_id][1].candidate_id != after_items[item_id][1].candidate_id
        for item_id in impacted_item_ids
    ):
        return False

    def protected_items(plan: TripPlan, service_date: date) -> tuple[ItineraryItem, ...]:
        day = next(item for item in plan.days if item.date == service_date)
        return tuple(
            item
            for item in day.items
            if item.item_id not in impacted_item_ids
            and item.candidate_id not in impacted_candidate_ids
        )

    return all(
        protected_items(before, day.date) == protected_items(after, day.date)
        and day.weather_risk_ids
        == next(item for item in after.days if item.date == day.date).weather_risk_ids
        for day in before.days
    )


def _empty_change(plan: TripPlan) -> WeatherPlanChange:
    return grade_weather_change(plan, plan)


def _stable_error_code(prefix: str, error: Exception | None = None) -> str:
    material = prefix
    if error is not None:
        material = f"{prefix}|{error.__class__.__module__}|{error.__class__.__qualname__}"
    return f"weather-replan-{hashlib.sha256(material.encode()).hexdigest()[:12]}"


async def run_weather_repair(
    request: TripRequest,
    plan: TripPlan,
    materials: PlanningMaterialBundle,
    opening_hours: OpeningHoursEvidenceBundle,
    latest_risks: tuple[WeatherRisk, ...],
    executor: WeatherReplanExecutor,
    *,
    validator: HardPlanValidator = validate_hard_trip_plan,
    max_attempts: int = MAX_WEATHER_REPLAN_ATTEMPTS,
) -> WeatherRepairResult:
    """Create and execute a bounded local replan when provider weather affects the trip."""

    if not 1 <= max_attempts <= MAX_WEATHER_REPLAN_ATTEMPTS:
        raise ValueError("weather replan attempt limit must be one or two")
    if {
        request.request_id,
        plan.request_id,
        materials.request_id,
        opening_hours.request_id,
    } != {request.request_id}:
        raise ValueError("weather repair inputs must share one request identity")
    impacts = detect_weather_impacts(plan, materials, latest_risks)
    if not impacts:
        return WeatherRepairResult(
            request_id=request.request_id,
            outcome=WeatherRepairOutcome.NO_ACTION,
            stop_reason=WeatherRepairStopReason.NO_SIGNIFICANT_IMPACT,
            impacts=(),
            initial_plan=plan,
            effective_plan=plan,
            change=_empty_change(plan),
            attempts=(),
            requires_user_confirmation=False,
            total_model_call_count=0,
            total_provider_call_count=0,
        )

    task = _task(request, plan, impacts, max_attempts)
    attempts: list[WeatherRepairAttemptTrace] = []
    latest_change = _empty_change(plan)
    latest_validation: PlanValidationReport | None = None
    for attempt_index in range(1, max_attempts + 1):
        try:
            execution = await executor.replan(
                request,
                plan,
                materials,
                opening_hours,
                task,
                impacts,
                attempt_index,
            )
        except Exception as error:
            execution = WeatherReplanExecutionResult(
                status=WeatherReplanExecutionStatus.FAILED,
                error_code=_stable_error_code("executor_exception", error),
            )
        if execution.status == WeatherReplanExecutionStatus.FAILED:
            attempts.append(
                WeatherRepairAttemptTrace(
                    attempt_index=attempt_index,
                    execution_status=execution.status,
                    scope_valid=False,
                    remaining_impact_count=len(impacts),
                    change=_empty_change(plan),
                    model_call_count=execution.model_call_count,
                    provider_call_count=execution.provider_call_count,
                    error_code=execution.error_code,
                )
            )
            continue

        assert execution.proposed_plan is not None
        proposal = execution.proposed_plan
        latest_change = grade_weather_change(plan, proposal)
        scope_valid = _scope_is_valid(plan, proposal, impacts)
        remaining_impacts = detect_weather_impacts(proposal, materials, latest_risks)
        latest_validation = validator(request, proposal, materials, opening_hours)
        error_code: str | None = None
        if not scope_valid:
            error_code = _stable_error_code("scope_violation")
        elif latest_change.grade == WeatherChangeGrade.NONE:
            error_code = _stable_error_code("unchanged_proposal")
        elif remaining_impacts:
            error_code = _stable_error_code("weather_impact_remaining")
        elif not latest_validation.can_finalize:
            error_code = _stable_error_code("hard_validation_failed")
        attempt_status = (
            WeatherReplanExecutionStatus.FAILED
            if error_code is not None
            else WeatherReplanExecutionStatus.SUCCEEDED
        )
        attempts.append(
            WeatherRepairAttemptTrace(
                attempt_index=attempt_index,
                execution_status=attempt_status,
                scope_valid=scope_valid,
                remaining_impact_count=len(remaining_impacts),
                change=latest_change,
                validation_report=latest_validation,
                model_call_count=execution.model_call_count,
                provider_call_count=execution.provider_call_count,
                error_code=error_code,
            )
        )
        if error_code is not None:
            continue
        if latest_change.grade == WeatherChangeGrade.MAJOR:
            pending = proposal.model_copy(update={"status": PlanStatus.PENDING_CONFIRMATION})
            return WeatherRepairResult(
                request_id=request.request_id,
                outcome=WeatherRepairOutcome.WAITING_FOR_USER,
                stop_reason=WeatherRepairStopReason.USER_CONFIRMATION_REQUIRED,
                impacts=impacts,
                task=task,
                initial_plan=plan,
                effective_plan=plan,
                proposed_plan=pending,
                change=latest_change,
                validation_report=latest_validation,
                attempts=tuple(attempts),
                requires_user_confirmation=True,
                total_model_call_count=sum(item.model_call_count for item in attempts),
                total_provider_call_count=sum(item.provider_call_count for item in attempts),
            )
        return WeatherRepairResult(
            request_id=request.request_id,
            outcome=WeatherRepairOutcome.AUTO_APPLIED,
            stop_reason=WeatherRepairStopReason.FINALIZABLE,
            impacts=impacts,
            task=task,
            initial_plan=plan,
            effective_plan=proposal,
            proposed_plan=proposal,
            change=latest_change,
            validation_report=latest_validation,
            attempts=tuple(attempts),
            requires_user_confirmation=False,
            total_model_call_count=sum(item.model_call_count for item in attempts),
            total_provider_call_count=sum(item.provider_call_count for item in attempts),
        )

    return WeatherRepairResult(
        request_id=request.request_id,
        outcome=WeatherRepairOutcome.UNRESOLVED,
        stop_reason=WeatherRepairStopReason.RETRY_LIMIT_REACHED,
        impacts=impacts,
        task=task,
        initial_plan=plan,
        effective_plan=plan,
        change=latest_change,
        validation_report=latest_validation,
        attempts=tuple(attempts),
        requires_user_confirmation=False,
        total_model_call_count=sum(item.model_call_count for item in attempts),
        total_provider_call_count=sum(item.provider_call_count for item in attempts),
    )
