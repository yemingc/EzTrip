import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from time import perf_counter
from typing import NotRequired, Protocol, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import ValidationError

from app.agents.contracts import (
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerProposalBatch,
)
from app.agents.plan_agent_contracts import (
    PlanAgentDecision,
    PlanAgentRunResult,
    PlanAgentRunStatus,
    PlanAgentSkipReason,
)
from app.core.config import Settings
from app.domain.candidates import CandidatePOI
from app.domain.planning import (
    ActivityKind,
    DayPlan,
    ItineraryItem,
    MealRecommendation,
    PlanStatus,
    TripPlan,
)
from app.domain.request import TripRequest
from app.domain.travel_data import WeatherRisk
from app.itinerary_quality import (
    MAX_MEAL_RECOMMENDATION_DISTANCE_METERS,
    straight_line_distance_meters,
)
from app.observability.probe import build_langsmith_client, require_secret
from app.observability.redaction import TraceRedactor
from app.planning.context_compiler import compile_planner_context
from app.planning.material_contracts import (
    PlanningMaterialBundle,
    PlanningMaterialStatus,
    RouteEdgeStatus,
    RouteMatrixEdge,
)
from app.planning.specialist_contracts import SpecialistName
from app.planning.validator import validate_trip_plan

PLAN_AGENT_NAME = "eztrip-multi-agent-plan-v1"
PLAN_AGENT_PROMPT_VERSION = "route-weather-budget-schedule-v1"
PLAN_AGENT_TOOL_NAME = "submit_grounded_schedule"
DEFAULT_ACTIVITY_DURATION_MINUTES = 120
CHINA_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

SYSTEM_PROMPT = """你是 EzTrip 多 Agent 工作流中的 Plan Agent, 只能安排输入 shortlist 中的 POI。
必须把每个 allowed_poi_candidate_id 恰好安排一次, 不得新增、改名、遗漏、安排酒店 ID 或餐饮候选。
你只决定 candidate_id、day_number、start_time 和简短 reason。
候选事实、日期、结束时间、来源、路线、天气、预算和稳定 ID 全部由代码回填。
assigned_day_clusters 已由代码按地理位置分组并排序;
day_number 必须匹配分组, 同日 start_time 必须保持该顺序。
综合路线时长、逐日天气风险、同行人、已确认约束和预算目标, 做软权衡排程。
预算 allocation 只是规划目标, 不是报价; 不得声称真实票价、酒店价格、可订状态、
实时营业时间或预算一定可行。
day_number 必须来自输入日期, start_time 只能是 08:00 到 21:30 的整点或半点。
必须调用 submit_grounded_schedule, 不要输出正文。"""


class PlanAgentConfigurationError(RuntimeError):
    """Raised when a live Plan Agent dependency is not configured."""


class PlanAgentProtocolError(RuntimeError):
    """Raised when a Plan Agent proposal breaks a deterministic boundary."""


class PlanProposalModel(Protocol):
    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse: ...


class PlanAgentState(TypedDict):
    request: TripRequest
    materials: PlanningMaterialBundle
    model_response: NotRequired[PlannerModelResponse]
    result: NotRequired[PlanAgentRunResult]


PLAN_AGENT_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": PLAN_AGENT_TOOL_NAME,
            "description": "提交只引用已给定 POI candidate_id 的逐日排程。",
            "parameters": PlannerProposalBatch.model_json_schema(mode="validation"),
        },
    },
)


def _weather_risks(materials: PlanningMaterialBundle) -> tuple[WeatherRisk, ...]:
    branch = next(
        item
        for item in materials.specialist_result.branches
        if item.specialist == SpecialistName.WEATHER
    )
    trip_start = materials.planner_context.start_date
    trip_end = materials.planner_context.end_date
    return tuple(
        risk
        for risk in branch.weather_risks
        if risk.ends_at.date() >= trip_start and risk.starts_at.date() <= trip_end
    )


def planning_material_sha256(materials: PlanningMaterialBundle) -> str:
    semantic_payload = {
        "bundle_version": materials.bundle_version,
        "request_id": materials.request_id,
        "context_id": materials.context_id,
        "data_mode": materials.data_mode.value,
        "status": materials.status.value,
        "issues": [item.value for item in materials.issues],
        "planner_context": materials.planner_context.model_dump(mode="json"),
        "shortlist": materials.shortlist.model_dump(mode="json"),
        "route_matrix": materials.route_matrix.model_dump(
            mode="json",
            exclude={"latency_ms"},
        ),
        "budget_allocation": materials.budget_allocation.model_dump(mode="json"),
        "weather_risks": [item.model_dump(mode="json") for item in _weather_risks(materials)],
    }
    canonical = json.dumps(
        semantic_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _material_input_payload(materials: PlanningMaterialBundle) -> str:
    context = materials.planner_context
    constraints = (
        *context.confirmed_hard_constraints,
        *context.confirmed_soft_constraints,
    )
    weather_risks = _weather_risks(materials)
    payload = {
        "destination": context.destination.normalized_name,
        "days": [
            {"day_number": item.day_number, "date": item.date.isoformat()} for item in context.days
        ],
        "party": {
            "adults": context.party.adults,
            "children": context.party.children,
            "seniors": context.party.seniors,
        },
        "travel_styles": list(context.travel_styles),
        "pace": context.pace.value if context.pace is not None else "unspecified",
        "activity_target_per_day": materials.shortlist.activity_target_per_day,
        "confirmed_constraints": [
            {
                "kind": item.kind.value,
                "value": item.value,
                "strength": item.strength.value,
                "priority": item.priority,
                "applies_to_dates": [value.isoformat() for value in item.applies_to_dates],
            }
            for item in constraints
        ],
        "allowed_poi_candidate_ids": [
            item.candidate_id for item in materials.shortlist.poi_candidates
        ],
        "poi_candidates": [
            {
                "candidate_id": item.candidate_id,
                "name": item.name,
                "district": item.district,
                "categories": list(item.categories),
                "environment": item.environment.value,
                "suggested_duration_minutes": item.suggested_duration_minutes,
                "tags": list(item.tags),
            }
            for item in materials.shortlist.poi_candidates
        ],
        "assigned_day_clusters": [
            {
                "day_number": item.day_number,
                "candidate_ids_in_route_order": list(item.poi_candidate_ids),
            }
            for item in materials.shortlist.day_clusters
        ],
        "meal_candidate_count": len(materials.shortlist.meal_candidates),
        "meal_candidates_are_scheduled_by_model": False,
        "stay_route_anchor": (
            {
                "candidate_id": materials.shortlist.primary_stay.candidate_id,
                "name": materials.shortlist.primary_stay.name,
                "area_name": materials.shortlist.primary_stay.area_name,
                "availability_status": materials.shortlist.primary_stay.availability_status,
                "booking_supported": materials.shortlist.primary_stay.booking_supported,
            }
            if materials.shortlist.primary_stay is not None
            else None
        ),
        "route_edges": [
            {
                "origin_candidate_id": edge.origin_candidate_id,
                "destination_candidate_id": edge.destination_candidate_id,
                "status": edge.status.value,
                "duration_minutes": edge.route.duration_minutes if edge.route else None,
                "distance_meters": edge.route.distance_meters if edge.route else None,
            }
            for edge in materials.route_matrix.edges
        ],
        "weather_risks": [
            {
                "risk_id": risk.risk_id,
                "starts_at": risk.starts_at.isoformat(),
                "ends_at": risk.ends_at.isoformat(),
                "risk_type": risk.risk_type.value,
                "severity": risk.severity.value,
                "affected_activity_types": list(risk.affected_activity_types),
                "advisory": risk.advisory,
            }
            for risk in weather_risks
        ],
        "budget": {
            "semantics": "planning_targets_not_verified_prices",
            "total_limit": str(materials.budget_allocation.total_limit),
            "hard_limit": materials.budget_allocation.hard_limit,
            "allocations": [
                {
                    "category": item.category.value,
                    "target_amount": str(item.target_amount),
                    "quantity_basis": item.quantity_basis.value,
                    "reference_quantity": str(item.reference_quantity),
                    "target_per_unit": str(item.target_per_unit),
                }
                for item in materials.budget_allocation.allocations
            ],
        },
        "truth_boundaries": {
            "opening_hours_verified": False,
            "stay_availability_verified": False,
            "booking_supported": False,
            "budget_targets_are_prices": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class DeepSeekPlanProposalModel:
    """DeepSeek adapter whose output schema cannot mutate upstream facts."""

    def __init__(self, settings: Settings) -> None:
        try:
            api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        except RuntimeError as error:
            raise PlanAgentConfigurationError(str(error)) from error
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekMultiAgentPlanProposal")
        self._model = settings.deepseek_model

    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _material_input_payload(materials)},
            ],
        )
        started = perf_counter()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[PLAN_AGENT_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": PLAN_AGENT_TOOL_NAME}},
            temperature=0,
            max_tokens=2200,
            extra_body={"thinking": {"type": "disabled"}},
        )
        latency_ms = round((perf_counter() - started) * 1000)
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls is None or len(tool_calls) != 1:
            raise PlanAgentProtocolError("DeepSeek must return exactly one Plan Agent tool call")
        tool_call = tool_calls[0]
        if tool_call.type != "function" or tool_call.function.name != PLAN_AGENT_TOOL_NAME:
            raise PlanAgentProtocolError("DeepSeek returned an unexpected Plan Agent tool call")
        try:
            proposal = PlannerProposalBatch.model_validate_json(tool_call.function.arguments)
        except ValidationError as error:
            raise PlanAgentProtocolError(
                "DeepSeek returned invalid Plan Agent arguments"
            ) from error
        usage = None
        if response.usage is not None:
            usage = ModelTokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
        return PlannerModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )


def _stable_id(prefix: str, *values: object) -> str:
    material = "|".join(str(value) for value in values)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _route_edges_by_pair(
    materials: PlanningMaterialBundle,
) -> dict[tuple[str, str], RouteMatrixEdge]:
    return {
        (edge.origin_candidate_id, edge.destination_candidate_id): edge
        for edge in materials.route_matrix.edges
    }


def _meal_recommendations(
    context_id: str,
    day: date,
    anchors: tuple[CandidatePOI, ...],
    candidates: tuple[CandidatePOI, ...],
) -> tuple[MealRecommendation, ...]:
    if not anchors:
        return ()
    ranked: list[tuple[int, CandidatePOI, CandidatePOI]] = []
    for candidate in candidates:
        anchor = min(
            anchors,
            key=lambda item: straight_line_distance_meters(
                item.location,
                candidate.location,
            ),
        )
        distance = straight_line_distance_meters(anchor.location, candidate.location)
        if distance <= MAX_MEAL_RECOMMENDATION_DISTANCE_METERS:
            ranked.append((distance, candidate, anchor))
    selected = sorted(
        ranked,
        key=lambda item: (item[0], item[1].candidate_id),
    )[:2]
    recommendations: list[MealRecommendation] = []
    for distance, candidate, anchor in selected:
        recommendations.append(
            MealRecommendation(
                recommendation_id=_stable_id(
                    "meal-recommendation",
                    context_id,
                    day.isoformat(),
                    anchor.candidate_id,
                    candidate.candidate_id,
                ),
                anchor_candidate_id=anchor.candidate_id,
                candidate=candidate,
                straight_line_distance_meters=distance,
                reason=f"距当日景点“{anchor.name}”约 {distance} 米的 Provider 候选。",
            )
        )
    return tuple(recommendations)


def _free_time_item(context_id: str, day: date) -> ItineraryItem:
    starts_at = datetime.combine(day, time(9, 0), tzinfo=CHINA_TIMEZONE)
    return ItineraryItem(
        item_id=_stable_id("plan-free-time", context_id, day.isoformat()),
        kind=ActivityKind.FREE_TIME,
        title="自由安排",
        start_at=starts_at,
        end_at=starts_at + timedelta(hours=1),
        notes=("本日暂无 shortlist 景点排程, 保留为空白草案, 不代表新增推荐事实。",),
    )


def _plan_id(
    request: TripRequest,
    materials: PlanningMaterialBundle,
    days: tuple[DayPlan, ...],
) -> str:
    payload = {
        "request_id": request.request_id,
        "context_id": materials.context_id,
        "materials": planning_material_sha256(materials),
        "days": [item.model_dump(mode="json") for item in days],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _stable_id("trip-plan", canonical)


def _validate_inputs(request: TripRequest, materials: PlanningMaterialBundle) -> None:
    expected_context = compile_planner_context(request)
    if request.request_id != materials.request_id or expected_context != materials.planner_context:
        raise PlanAgentProtocolError("Plan Agent request does not match its planning materials")


def _skipped_result(materials: PlanningMaterialBundle) -> PlanAgentRunResult:
    return PlanAgentRunResult(
        request_id=materials.request_id,
        context_id=materials.context_id,
        input_material_sha256=planning_material_sha256(materials),
        material_status=materials.status,
        budget_status=materials.budget_allocation.status,
        status=PlanAgentRunStatus.SKIPPED,
        skip_reason=PlanAgentSkipReason.MATERIALS_NOT_READY,
        input_poi_candidate_ids=tuple(
            item.candidate_id for item in materials.shortlist.poi_candidates
        ),
        primary_stay_candidate_id=(
            materials.shortlist.primary_stay.candidate_id
            if materials.shortlist.primary_stay is not None
            else None
        ),
        input_route_edge_count=len(materials.route_matrix.edges),
        input_weather_risk_ids=tuple(item.risk_id for item in _weather_risks(materials)),
        latency_ms=0,
        model_call_count=0,
    )


def normalize_plan_response(
    request: TripRequest,
    materials: PlanningMaterialBundle,
    response: PlannerModelResponse,
) -> PlanAgentRunResult:
    if materials.status != PlanningMaterialStatus.READY:
        raise PlanAgentProtocolError("Plan Agent normalizer requires ready planning materials")
    candidates = materials.shortlist.poi_candidates
    stay = materials.shortlist.primary_stay
    if not candidates or stay is None:
        raise PlanAgentProtocolError("ready planning materials require POIs and a stay anchor")
    candidates_by_id = {item.candidate_id: item for item in candidates}
    proposal_ids = [item.candidate_id for item in response.proposal.items]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise PlanAgentProtocolError("Plan Agent proposal repeats a candidate id")
    if set(proposal_ids) != set(candidates_by_id):
        raise PlanAgentProtocolError("Plan Agent must place every shortlist POI exactly once")

    proposals_by_id = {item.candidate_id: item for item in response.proposal.items}
    assigned_days = {
        candidate_id: cluster.day_number
        for cluster in materials.shortlist.day_clusters
        for candidate_id in cluster.poi_candidate_ids
    }
    for proposal in response.proposal.items:
        if request.pace is not None and proposal.day_number != assigned_days.get(
            proposal.candidate_id
        ):
            raise PlanAgentProtocolError("Plan Agent must preserve geographic day assignments")
        candidate = candidates_by_id[proposal.candidate_id]
        if candidate.city != materials.planner_context.destination.normalized_name:
            raise PlanAgentProtocolError("Plan Agent candidate city does not match the context")

    edges_by_pair = _route_edges_by_pair(materials)
    weather_risks = _weather_risks(materials)
    decisions: list[PlanAgentDecision] = []
    day_plans: list[DayPlan] = []
    clusters_by_day = {item.day_number: item for item in materials.shortlist.day_clusters}
    for context_day in materials.planner_context.days:
        day = context_day.date
        cluster = clusters_by_day[context_day.day_number]
        day_candidate_ids = (
            cluster.poi_candidate_ids
            if request.pace is not None
            else tuple(
                item.candidate_id
                for item in sorted(
                    (
                        item
                        for item in response.proposal.items
                        if item.day_number == context_day.day_number
                    ),
                    key=lambda item: (item.start_time, item.candidate_id),
                )
            )
        )
        day_decisions: list[PlanAgentDecision] = []
        previous_candidate_id = stay.candidate_id
        previous_end: datetime | None = None
        departure_from_stay_at: datetime | None = None
        day_candidates: list[CandidatePOI] = []
        for candidate_id in day_candidate_ids:
            proposal = proposals_by_id[candidate_id]
            candidate = candidates_by_id[candidate_id]
            hour, minute = (int(value) for value in proposal.start_time.split(":"))
            proposed_start = datetime.combine(day, time(hour, minute), tzinfo=CHINA_TIMEZONE)
            duration = candidate.suggested_duration_minutes or DEFAULT_ACTIVITY_DURATION_MINUTES
            edge = edges_by_pair.get((previous_candidate_id, candidate.candidate_id))
            if edge is None or edge.status != RouteEdgeStatus.SUCCEEDED or edge.route is None:
                raise PlanAgentProtocolError("Plan Agent schedule references a missing route edge")
            if previous_end is None:
                earliest_departure = datetime.combine(day, time(7), tzinfo=CHINA_TIMEZONE)
                earliest_start = earliest_departure + timedelta(minutes=edge.route.duration_minutes)
            else:
                earliest_start = previous_end + timedelta(minutes=edge.route.duration_minutes)
            starts_at = max(proposed_start, earliest_start)
            ends_at = starts_at + timedelta(minutes=duration)
            if ends_at.date() != day:
                raise PlanAgentProtocolError("Plan Agent activity cannot cross the day boundary")
            if departure_from_stay_at is None:
                departure_from_stay_at = starts_at - timedelta(minutes=edge.route.duration_minutes)
            item = ItineraryItem(
                item_id=_stable_id(
                    "plan-item",
                    materials.context_id,
                    candidate.candidate_id,
                    starts_at.isoformat(),
                ),
                kind=ActivityKind.ATTRACTION,
                title=candidate.name,
                start_at=starts_at,
                end_at=ends_at,
                candidate_id=candidate.candidate_id,
                source=candidate.source,
                route_from_previous=edge.route,
                notes=(
                    "活动事实与来源由 shortlist 候选回填, 模型只负责排程提案。",
                    "预算额度仅作排程目标; 实时价格、营业时间和可订状态尚未验证。",
                ),
            )
            decision = PlanAgentDecision(
                proposal=proposal,
                item=item,
                route_edge_id=edge.edge_id,
            )
            day_decisions.append(decision)
            day_candidates.append(candidate)
            previous_candidate_id = candidate.candidate_id
            previous_end = ends_at
        items = tuple(item.item for item in day_decisions) or (
            _free_time_item(materials.context_id, day),
        )
        meal_recommendations = _meal_recommendations(
            materials.context_id,
            day,
            tuple(day_candidates),
            materials.shortlist.meal_candidates,
        )
        risk_ids = tuple(
            risk.risk_id
            for risk in weather_risks
            if risk.starts_at.date() <= day <= risk.ends_at.date()
        )
        try:
            day_plan = DayPlan(
                date=day,
                items=items,
                departure_from_stay_at=departure_from_stay_at,
                meal_recommendations=meal_recommendations,
                weather_risk_ids=risk_ids,
            )
        except ValidationError as error:
            raise PlanAgentProtocolError(
                "Plan Agent proposal creates an invalid timeline"
            ) from error
        decisions.extend(day_decisions)
        day_plans.append(day_plan)

    days = tuple(day_plans)
    plan = TripPlan(
        plan_id=_plan_id(request, materials, days),
        request_id=request.request_id,
        status=PlanStatus.DRAFT,
        destination_city=materials.planner_context.destination.normalized_name,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        cost_items=(),
        weather_risks=weather_risks,
    )
    validation = validate_trip_plan(request, plan)
    return PlanAgentRunResult(
        request_id=request.request_id,
        context_id=materials.context_id,
        input_material_sha256=planning_material_sha256(materials),
        material_status=materials.status,
        budget_status=materials.budget_allocation.status,
        status=PlanAgentRunStatus.PLANNED,
        input_poi_candidate_ids=tuple(item.candidate_id for item in candidates),
        primary_stay_candidate_id=stay.candidate_id,
        input_route_edge_count=len(materials.route_matrix.edges),
        input_weather_risk_ids=tuple(item.risk_id for item in weather_risks),
        model=response.model,
        latency_ms=response.latency_ms,
        usage=response.usage,
        model_call_count=1,
        decisions=tuple(decisions),
        route_edge_ids_used=tuple(item.route_edge_id for item in decisions),
        plan=plan,
        validation=validation,
    )


def build_plan_agent_graph(
    model: PlanProposalModel,
) -> CompiledStateGraph[PlanAgentState, None, PlanAgentState, PlanAgentState]:
    def propose_schedule(state: PlanAgentState) -> Mapping[str, PlannerModelResponse]:
        return {"model_response": model.propose(state["materials"])}

    def normalize_schedule(state: PlanAgentState) -> Mapping[str, PlanAgentRunResult]:
        response = state.get("model_response")
        if response is None:
            raise PlanAgentProtocolError("Plan Agent normalizer received no model response")
        return {
            "result": normalize_plan_response(
                state["request"],
                state["materials"],
                response,
            )
        }

    workflow = StateGraph(PlanAgentState)
    workflow.add_node("propose_schedule", propose_schedule)
    workflow.add_node("normalize_schedule", normalize_schedule)
    workflow.add_edge(START, "propose_schedule")
    workflow.add_edge("propose_schedule", "normalize_schedule")
    workflow.add_edge("normalize_schedule", END)
    return workflow.compile(checkpointer=False, name=PLAN_AGENT_NAME)


def build_plan_agent_run_config(materials: PlanningMaterialBundle, *, model: str) -> RunnableConfig:
    return {
        "run_name": PLAN_AGENT_NAME,
        "tags": ["ez-302", "plan-agent", "schema-constrained", "multi-agent"],
        "metadata": {
            "agent_version": "multi-agent-plan-v1",
            "prompt_version": PLAN_AGENT_PROMPT_VERSION,
            "request_id": materials.request_id,
            "context_id": materials.context_id,
            "material_status": materials.status.value,
            "model": model,
            "raw_user_text_in_metadata": False,
        },
    }


def run_plan_agent(
    request: TripRequest,
    materials: PlanningMaterialBundle,
    model: PlanProposalModel,
) -> PlanAgentRunResult:
    _validate_inputs(request, materials)
    if materials.status != PlanningMaterialStatus.READY:
        return _skipped_result(materials)
    graph = build_plan_agent_graph(model)
    final_state = cast(
        PlanAgentState,
        graph.invoke(
            {"request": request, "materials": materials},
            config=build_plan_agent_run_config(materials, model="injected-model"),
        ),
    )
    result = final_state.get("result")
    if result is None:
        raise PlanAgentProtocolError("Plan Agent completed without a result")
    return result


def run_live_plan_agent(
    request: TripRequest,
    materials: PlanningMaterialBundle,
    settings: Settings,
) -> PlanAgentRunResult:
    _validate_inputs(request, materials)
    if materials.status != PlanningMaterialStatus.READY:
        return _skipped_result(materials)
    if not settings.langsmith_tracing:
        raise PlanAgentConfigurationError("LANGSMITH_TRACING must be true for the live Plan Agent")
    redactor = TraceRedactor.from_settings(settings)
    try:
        langsmith_client = build_langsmith_client(settings, redactor)
    except RuntimeError as error:
        raise PlanAgentConfigurationError(str(error)) from error
    model = DeepSeekPlanProposalModel(settings)
    graph = build_plan_agent_graph(model)
    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            final_state = cast(
                PlanAgentState,
                graph.invoke(
                    {"request": request, "materials": materials},
                    config=build_plan_agent_run_config(
                        materials,
                        model=settings.deepseek_model,
                    ),
                ),
            )
    finally:
        langsmith_client.flush(timeout=15.0)
    result = final_state.get("result")
    if result is None:
        raise PlanAgentProtocolError("live Plan Agent completed without a result")
    return result
