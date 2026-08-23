import hashlib
from datetime import UTC, datetime, time, timedelta, timezone

from app.agents.contracts import (
    ExploreCandidateObservation,
    ExploreCandidateSelectionProposal,
    ExploreEvidenceKind,
    ExploreEvidenceReference,
    ExploreQueryKind,
    ExploreQueryModelResponse,
    ExploreQueryProposal,
    ExploreQueryProposalBatch,
    ExploreSearchQuery,
    ExploreSelectionModelResponse,
    ExploreSelectionProposalBatch,
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
    StayCandidateObservation,
    StayCandidateSelectionProposal,
    StayEvidenceKind,
    StayEvidenceReference,
    StayQueryModelResponse,
    StayQueryProposal,
    StayQueryProposalBatch,
    StaySearchQuery,
    StaySelectionModelResponse,
    StaySelectionProposalBatch,
)
from app.agents.plan_agent import run_plan_agent
from app.agents.plan_agent_contracts import PlanAgentRunResult
from app.domain.candidates import ActivityEnvironment, CandidatePOI, CandidateStay, GeoPoint
from app.domain.context import PlannerContext
from app.domain.opening_hours import OpeningHoursEvidence, OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import (
    RiskSeverity,
    RouteLeg,
    RouteMode,
    WeatherRisk,
    WeatherRiskType,
)
from app.planning.material_builder import build_planning_material_bundle
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.planning.specialist_fanout import run_specialist_fanout
from app.providers.ports import (
    POISearchRequest,
    RouteRequest,
    StaySearchRequest,
    WeatherRiskRequest,
)

CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
FIXTURE_RETRIEVED_AT = datetime(2026, 8, 21, tzinfo=UTC)


def _digest(*values: object) -> str:
    return hashlib.sha256("|".join(str(value) for value in values).encode("utf-8")).hexdigest()


def _source(provider: str, provider_id: str) -> SourceReference:
    return SourceReference(
        provider=provider,
        provider_id=provider_id,
        data_mode=DataMode.FIXTURE,
        retrieved_at=FIXTURE_RETRIEVED_AT,
        raw_response_sha256=_digest(provider, provider_id, "eztrip-product-fixture-v1"),
    )


def _poi_catalog() -> tuple[CandidatePOI, ...]:
    return (
        CandidatePOI(
            candidate_id="product-fixture-palace-museum",
            name="故宫博物院",
            city="北京市",
            district="东城区",
            address="景山前街4号",
            location=GeoPoint(latitude=39.9178, longitude=116.3970),
            categories=("博物馆", "世界遗产"),
            environment=ActivityEnvironment.MIXED,
            suggested_duration_minutes=180,
            tags=("历史文化", "雨天可优先室内展馆"),
            source=_source("eztrip-product-fixture", "BJ-PALACE-MUSEUM"),
        ),
        CandidatePOI(
            candidate_id="product-fixture-temple-of-heaven",
            name="天坛公园",
            city="北京市",
            district="东城区",
            address="天坛东里甲1号",
            location=GeoPoint(latitude=39.8819, longitude=116.4108),
            categories=("风景名胜", "公园"),
            environment=ActivityEnvironment.OUTDOOR,
            suggested_duration_minutes=150,
            tags=("历史文化", "户外步行"),
            source=_source("eztrip-product-fixture", "BJ-TEMPLE-OF-HEAVEN"),
        ),
    )


def _stay_catalog() -> tuple[CandidateStay, ...]:
    return (
        CandidateStay(
            candidate_id="product-fixture-qianmen-stay",
            name="前门示例住宿",
            city="北京市",
            district="东城区",
            address="前门示例路1号",
            location=GeoPoint(latitude=39.8992, longitude=116.3976),
            area_name="前门片区",
            tags=("中心城区", "仅为位置锚点"),
            source=_source("eztrip-product-fixture", "BJ-QIANMEN-STAY"),
        ),
    )


class ProductFixtureProvider:
    """Deterministic product-demo data; every value remains labelled as fixture."""

    def __init__(self, request: TripRequest) -> None:
        self._request = request

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        return _poi_catalog()[: request.limit]

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        return _stay_catalog()[: request.limit]

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        del request
        starts_at = datetime.combine(self._request.start_date, time.min, tzinfo=CHINA_TIMEZONE)
        return (
            WeatherRisk(
                risk_id=f"product-fixture-rain-{self._request.start_date.isoformat()}",
                city=self._request.destination_city,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(days=1),
                risk_type=WeatherRiskType.RAIN,
                severity=RiskSeverity.MEDIUM,
                threshold_description="产品 fixture 模拟首日中雨预报。",
                affected_activity_types=("outdoor",),
                advisory="首日优先室内或混合型活动, 户外活动移至次日。",
                source=_source(
                    "eztrip-product-weather-fixture",
                    f"rain-{self._request.start_date.isoformat()}",
                ),
            ),
        )

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        origin_id = request.origin.candidate_id or "origin"
        destination_id = request.destination.candidate_id or "destination"
        distance = round(
            (
                abs(request.origin.location.latitude - request.destination.location.latitude)
                + abs(request.origin.location.longitude - request.destination.location.longitude)
            )
            * 100_000
        )
        digest = _digest(origin_id, destination_id, request.mode.value)
        return RouteLeg(
            route_leg_id=f"product-fixture-route-{digest[:16]}",
            origin=request.origin,
            destination=request.destination,
            mode=RouteMode.TRANSIT,
            distance_meters=max(distance, 100),
            duration_minutes=max(round(distance / 350), 1),
            source=SourceReference(
                provider="eztrip-product-route-fixture",
                provider_id=f"route-{digest[:12]}",
                data_mode=DataMode.FIXTURE,
                retrieved_at=FIXTURE_RETRIEVED_AT,
                raw_response_sha256=digest,
            ),
        )


class ProductFixtureExploreModel:
    def propose_queries(self, context: PlannerContext) -> ExploreQueryModelResponse:
        return ExploreQueryModelResponse(
            proposal=ExploreQueryProposalBatch(
                items=(
                    ExploreQueryProposal(
                        kind=ExploreQueryKind.ATTRACTION,
                        keywords=f"{context.destination.normalized_name}历史文化景点",
                        reason="覆盖产品演示中的已确认必去地点与历史文化偏好。",
                    ),
                )
            ),
            model="product-fixture-explore-model-v1",
            latency_ms=0,
        )

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[ExploreSearchQuery, ...],
        observations: tuple[ExploreCandidateObservation, ...],
    ) -> ExploreSelectionModelResponse:
        del context, queries
        return ExploreSelectionModelResponse(
            proposal=ExploreSelectionProposalBatch(
                items=tuple(
                    ExploreCandidateSelectionProposal(
                        candidate_id=item.candidate.candidate_id,
                        rank=index,
                        reason="候选命中已确认的产品演示必去地点。",
                        evidence=(
                            ExploreEvidenceReference(
                                kind=ExploreEvidenceKind.CATEGORY,
                                value=item.candidate.categories[0],
                            ),
                        ),
                    )
                    for index, item in enumerate(observations, start=1)
                )
            ),
            model="product-fixture-explore-model-v1",
            latency_ms=0,
        )


class ProductFixtureStayModel:
    def propose_queries(self, context: PlannerContext) -> StayQueryModelResponse:
        return StayQueryModelResponse(
            proposal=StayQueryProposalBatch(
                items=(
                    StayQueryProposal(
                        target_area="前门片区",
                        keywords=f"{context.destination.normalized_name}前门住宿",
                        reason="选择中心城区住宿位置作为路线锚点。",
                    ),
                )
            ),
            model="product-fixture-stay-model-v1",
            latency_ms=0,
        )

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[StaySearchQuery, ...],
        observations: tuple[StayCandidateObservation, ...],
    ) -> StaySelectionModelResponse:
        del context, queries
        return StaySelectionModelResponse(
            proposal=StaySelectionProposalBatch(
                items=tuple(
                    StayCandidateSelectionProposal(
                        candidate_id=item.candidate.candidate_id,
                        rank=index,
                        reason="中心城区位置适合作为路线矩阵住宿锚点。",
                        evidence=(
                            StayEvidenceReference(
                                kind=StayEvidenceKind.AREA_NAME,
                                value=item.candidate.area_name,
                            ),
                        ),
                    )
                    for index, item in enumerate(observations, start=1)
                )
            ),
            model="product-fixture-stay-model-v1",
            latency_ms=0,
        )


class ProductFixturePlanModel:
    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        candidates = materials.shortlist.poi_candidates
        rainy_dates = {
            risk.starts_at.date()
            for branch in materials.specialist_result.branches
            for risk in branch.weather_risks
            if risk.risk_type == WeatherRiskType.RAIN
        }
        dry_day_number = next(
            (
                day.day_number
                for day in materials.planner_context.days
                if day.date not in rainy_dates
            ),
            1,
        )
        rainy_day_number = next(
            (day.day_number for day in materials.planner_context.days if day.date in rainy_dates),
            1,
        )
        proposals = tuple(
            PlannerPlacementProposal(
                candidate_id=candidate.candidate_id,
                day_number=(
                    dry_day_number
                    if candidate.environment == ActivityEnvironment.OUTDOOR
                    else rainy_day_number
                ),
                start_time="09:00",
                reason=(
                    "天气分支提示降雨, 将户外活动安排到无雨日期。"
                    if candidate.environment == ActivityEnvironment.OUTDOOR
                    else "降雨日优先安排室内或混合型活动。"
                ),
            )
            for candidate in candidates
        )
        return PlannerModelResponse(
            proposal=PlannerProposalBatch(items=proposals),
            model="product-fixture-plan-model-v1",
            latency_ms=0,
        )


class FixtureProductPlanningPipeline:
    def __init__(self, request: TripRequest) -> None:
        self._provider = ProductFixtureProvider(request)
        self._explore_model = ProductFixtureExploreModel()
        self._stay_model = ProductFixtureStayModel()
        self._plan_model = ProductFixturePlanModel()

    async def run_specialists(
        self,
        request: TripRequest,
        *,
        data_mode: DataMode,
    ) -> SpecialistFanoutResult:
        return await run_specialist_fanout(
            request,
            self._provider,
            self._explore_model,
            self._stay_model,
            data_mode=data_mode,
        )

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle:
        return await build_planning_material_bundle(specialist_result, self._provider)

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult:
        return run_plan_agent(request, materials, self._plan_model)

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle:
        items = tuple(
            OpeningHoursEvidence(
                evidence_id=f"product-fixture-hours-{item.item_id}",
                candidate_id=item.candidate_id,
                service_date=day.date,
                opens_at=datetime.combine(day.date, time(8), tzinfo=CHINA_TIMEZONE),
                closes_at=datetime.combine(day.date, time(18), tzinfo=CHINA_TIMEZONE),
                source=_source(
                    "eztrip-product-opening-hours-fixture",
                    f"{item.candidate_id}:{day.date.isoformat()}",
                ),
            )
            for day in plan.days
            for item in day.items
            if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
            and item.candidate_id is not None
        )
        return OpeningHoursEvidenceBundle(
            request_id=request.request_id,
            data_mode=data_mode,
            items=items,
        )
