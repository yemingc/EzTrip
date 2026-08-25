import hashlib
from datetime import UTC, datetime, time, timedelta, timezone

from app.agents.contracts import (
    ExploreAgentResult,
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
    StayAgentResult,
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
from app.agents.explore_agent import run_explore_agent
from app.agents.plan_agent import run_plan_agent
from app.agents.plan_agent_contracts import PlanAgentRunResult
from app.agents.stay_agent import run_stay_agent
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


def _fixture_pois(
    city: str,
    rows: tuple[tuple[str, str, str, float, float, str], ...],
) -> tuple[CandidatePOI, ...]:
    indoor_terms = ("博物馆", "博物院", "美术馆", "艺术宫", "天文馆")
    outdoor_terms = (
        "公园",
        "动物园",
        "繁育研究基地",
        "外滩",
        "滨水区",
        "步行街",
    )

    def environment_for(name: str, kind: str) -> ActivityEnvironment:
        if kind == "dining" or any(term in name for term in indoor_terms):
            return ActivityEnvironment.INDOOR
        if any(term in name for term in outdoor_terms):
            return ActivityEnvironment.OUTDOOR
        return ActivityEnvironment.MIXED

    return tuple(
        CandidatePOI(
            candidate_id=f"product-fixture-{slug}",
            name=name,
            city=city,
            district=district,
            address=f"{name} fixture 地址",
            location=GeoPoint(latitude=latitude, longitude=longitude),
            categories=(("餐饮服务", "本地美食") if kind == "dining" else ("景点", "城市体验")),
            environment=environment_for(name, kind),
            suggested_duration_minutes=90 if kind == "dining" else 120,
            tags=(("附近餐饮推荐",) if kind == "dining" else ("主要游览项目",)),
            source=_source("eztrip-product-fixture", slug.upper()),
        )
        for slug, name, district, latitude, longitude, kind in rows
    )


def _beijing_pois() -> tuple[CandidatePOI, ...]:
    return _fixture_pois(
        "北京市",
        (
            ("palace-museum", "故宫博物院", "东城区", 39.9178, 116.3970, "activity"),
            ("temple-of-heaven", "天坛公园", "东城区", 39.8819, 116.4108, "activity"),
            ("national-museum", "中国国家博物馆", "东城区", 39.9051, 116.4010, "activity"),
            ("jingshan-park", "景山公园", "西城区", 39.9251, 116.3965, "activity"),
            ("beihai-park", "北海公园", "西城区", 39.9255, 116.3890, "activity"),
            ("shichahai", "什刹海", "西城区", 39.9402, 116.3852, "activity"),
            ("gongwangfu", "恭王府", "西城区", 39.9371, 116.3863, "activity"),
            ("capital-museum", "首都博物馆", "西城区", 39.9054, 116.3430, "activity"),
            ("beijing-planetarium", "北京天文馆", "西城区", 39.9383, 116.3360, "activity"),
            ("nanluoguxiang", "南锣鼓巷", "东城区", 39.9372, 116.4034, "activity"),
            ("lama-temple", "雍和宫文化片区", "东城区", 39.9471, 116.4173, "activity"),
            ("summer-palace", "颐和园", "海淀区", 39.9999, 116.2755, "activity"),
            ("yuanmingyuan", "圆明园", "海淀区", 40.0081, 116.2984, "activity"),
            ("beijing-zoo", "北京动物园", "西城区", 39.9386, 116.3376, "activity"),
            ("olympic-park", "奥林匹克公园", "朝阳区", 40.0016, 116.3928, "activity"),
            ("798-art", "798 艺术区", "朝阳区", 39.9840, 116.4950, "activity"),
            ("bj-food-1", "前门本地餐饮示例一", "东城区", 39.8995, 116.3980, "dining"),
            ("bj-food-2", "故宫附近餐饮示例二", "东城区", 39.9188, 116.3990, "dining"),
            ("bj-food-3", "什刹海餐饮示例三", "西城区", 39.9390, 116.3860, "dining"),
            ("bj-food-4", "海淀餐饮示例四", "海淀区", 40.0005, 116.2800, "dining"),
            ("bj-food-5", "朝阳餐饮示例五", "朝阳区", 39.9850, 116.4920, "dining"),
        ),
    )


def _shanghai_pois() -> tuple[CandidatePOI, ...]:
    return _fixture_pois(
        "上海市",
        (
            ("shanghai-museum", "上海博物馆", "黄浦区", 31.2303, 121.4700, "activity"),
            ("yuyuan", "豫园", "黄浦区", 31.2272, 121.4921, "activity"),
            ("the-bund", "外滩", "黄浦区", 31.2400, 121.4900, "activity"),
            ("nanjing-road", "南京路步行街", "黄浦区", 31.2354, 121.4751, "activity"),
            ("xintiandi", "新天地", "黄浦区", 31.2192, 121.4753, "activity"),
            ("tianzifang", "田子坊", "黄浦区", 31.2080, 121.4680, "activity"),
            ("wukang-road", "武康路历史文化街区", "徐汇区", 31.2101, 121.4380, "activity"),
            ("natural-history", "上海自然博物馆", "静安区", 31.2330, 121.4540, "activity"),
            ("power-station-art", "上海当代艺术博物馆", "黄浦区", 31.2012, 121.4970, "activity"),
            ("china-art-palace", "中华艺术宫", "浦东新区", 31.1850, 121.4900, "activity"),
            ("pudong-art", "浦东美术馆", "浦东新区", 31.2422, 121.5010, "activity"),
            ("oriental-pearl", "东方明珠城市观景区", "浦东新区", 31.2397, 121.4998, "activity"),
            ("long-museum", "龙美术馆西岸馆", "徐汇区", 31.1840, 121.4490, "activity"),
            ("sinan-mansions", "思南公馆", "黄浦区", 31.2135, 121.4670, "activity"),
            ("north-bund", "北外滩滨水区", "虹口区", 31.2520, 121.4980, "activity"),
            ("sh-food-1", "人民广场餐饮示例一", "黄浦区", 31.2310, 121.4720, "dining"),
            ("sh-food-2", "豫园餐饮示例二", "黄浦区", 31.2268, 121.4910, "dining"),
            ("sh-food-3", "徐汇餐饮示例三", "徐汇区", 31.2090, 121.4400, "dining"),
            ("sh-food-4", "陆家嘴餐饮示例四", "浦东新区", 31.2400, 121.5005, "dining"),
            ("sh-food-5", "北外滩餐饮示例五", "虹口区", 31.2510, 121.4970, "dining"),
        ),
    )


def _chengdu_pois() -> tuple[CandidatePOI, ...]:
    return _fixture_pois(
        "成都市",
        (
            ("jinsha-museum", "金沙遗址博物馆", "青羊区", 30.6803, 104.0196, "activity"),
            ("panda-base", "成都大熊猫繁育研究基地", "成华区", 30.7381, 104.1471, "activity"),
            ("chengdu-museum", "成都博物馆", "青羊区", 30.6573, 104.0648, "activity"),
            ("people-park", "人民公园", "青羊区", 30.6570, 104.0550, "activity"),
            ("kuanzhai", "宽窄巷子", "青羊区", 30.6690, 104.0550, "activity"),
            ("dufu-cottage", "杜甫草堂", "青羊区", 30.6600, 104.0280, "activity"),
            ("sichuan-museum", "四川博物院", "青羊区", 30.6610, 104.0350, "activity"),
            ("wuhou-shrine", "武侯祠文化片区", "武侯区", 30.6460, 104.0470, "activity"),
            ("jinli", "锦里历史街区", "武侯区", 30.6450, 104.0480, "activity"),
            ("wangjiang-park", "望江楼公园", "武侯区", 30.6300, 104.0900, "activity"),
            ("east-suburb-memory", "东郊记忆", "成华区", 30.6710, 104.1200, "activity"),
            ("tianfu-art", "天府艺术公园", "金牛区", 30.7240, 104.0390, "activity"),
            ("jiuyanqiao", "九眼桥滨水区", "锦江区", 30.6400, 104.0900, "activity"),
            ("huanhuaxi", "浣花溪公园", "青羊区", 30.6570, 104.0300, "activity"),
            ("taikooli", "太古里城市街区", "锦江区", 30.6540, 104.0830, "activity"),
            ("cd-food-1", "天府广场餐饮示例一", "青羊区", 30.6575, 104.0660, "dining"),
            ("cd-food-2", "宽窄巷子餐饮示例二", "青羊区", 30.6680, 104.0560, "dining"),
            ("cd-food-3", "武侯餐饮示例三", "武侯区", 30.6465, 104.0490, "dining"),
            ("cd-food-4", "成华餐饮示例四", "成华区", 30.6740, 104.1190, "dining"),
            ("cd-food-5", "锦江餐饮示例五", "锦江区", 30.6530, 104.0840, "dining"),
        ),
    )


POI_CATALOGS = {
    "北京市": _beijing_pois(),
    "上海市": _shanghai_pois(),
    "成都市": _chengdu_pois(),
}


def _stay_catalog(city: str) -> tuple[CandidateStay, ...]:
    fixture = {
        "北京市": (
            "product-fixture-qianmen-stay",
            "前门示例住宿",
            "东城区",
            "前门示例路1号",
            39.8992,
            116.3976,
            "前门片区",
            "BJ-QIANMEN-STAY",
        ),
        "上海市": (
            "product-fixture-people-square-stay",
            "人民广场示例住宿",
            "黄浦区",
            "人民广场示例路1号",
            31.2320,
            121.4752,
            "人民广场片区",
            "SH-PEOPLE-SQUARE-STAY",
        ),
        "成都市": (
            "product-fixture-tianfu-square-stay",
            "天府广场示例住宿",
            "青羊区",
            "天府广场示例路1号",
            30.6570,
            104.0658,
            "天府广场片区",
            "CD-TIANFU-SQUARE-STAY",
        ),
    }[city]
    candidate_id, name, district, address, latitude, longitude, area_name, provider_id = fixture
    return (
        CandidateStay(
            candidate_id=candidate_id,
            name=name,
            city=city,
            district=district,
            address=address,
            location=GeoPoint(latitude=latitude, longitude=longitude),
            area_name=area_name,
            tags=("中心城区", "仅为位置锚点"),
            source=_source("eztrip-product-fixture", provider_id),
        ),
    )


class ProductFixtureProvider:
    """Deterministic product-demo data; every value remains labelled as fixture."""

    def __init__(self, request: TripRequest) -> None:
        self._request = request

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        catalog = POI_CATALOGS[self._request.destination_city]
        if "餐饮" in request.keywords:
            return catalog[-5:][: request.limit]
        page = next(
            (
                index
                for index, marker in enumerate(("主题一", "主题二", "主题三"))
                if marker in request.keywords
            ),
            0,
        )
        start = page * 5
        return catalog[start : start + request.limit]

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        return _stay_catalog(self._request.destination_city)[: request.limit]

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
                items=tuple(
                    ExploreQueryProposal(
                        kind=(
                            ExploreQueryKind.DINING
                            if marker == "餐饮"
                            else ExploreQueryKind.ATTRACTION
                        ),
                        keywords=f"{context.destination.normalized_name}{marker}",
                        reason=(
                            "提供与主要景点分离的附近餐饮建议池。"
                            if marker == "餐饮"
                            else "扩展多日主要游览项目候选池。"
                        ),
                    )
                    for marker in ("主题一", "主题二", "主题三", "餐饮")
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
        del queries
        selected_observations = observations[:2] if context.pace is None else observations
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
                    for index, item in enumerate(selected_observations, start=1)
                )
            ),
            model="product-fixture-explore-model-v1",
            latency_ms=0,
        )


class ProductFixtureStayModel:
    def propose_queries(self, context: PlannerContext) -> StayQueryModelResponse:
        target_area = {
            "北京市": "前门片区",
            "上海市": "人民广场片区",
            "成都市": "天府广场片区",
        }[context.destination.normalized_name]
        return StayQueryModelResponse(
            proposal=StayQueryProposalBatch(
                items=(
                    StayQueryProposal(
                        target_area=target_area,
                        keywords=f"{context.destination.normalized_name}{target_area.removesuffix('片区')}住宿",
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
        start_times = ("09:00", "13:00", "16:00", "19:00")
        proposals = tuple(
            PlannerPlacementProposal(
                candidate_id=candidate_id,
                day_number=cluster.day_number,
                start_time=start_times[index],
                reason="遵循 fixture 地理分组与路线顺序安排主要游览项目。",
            )
            for cluster in materials.shortlist.day_clusters
            for index, candidate_id in enumerate(cluster.poi_candidate_ids)
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

    async def rerun_explore(self, context: PlannerContext) -> ExploreAgentResult:
        return await run_explore_agent(context, self._provider, self._explore_model)

    async def rerun_stay(self, context: PlannerContext) -> StayAgentResult:
        return await run_stay_agent(context, self._provider, self._stay_model)

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
                opens_at=datetime.combine(
                    day.date,
                    (
                        time(10)
                        if item.candidate_id
                        in {
                            "product-fixture-temple-of-heaven",
                            "product-fixture-yuyuan",
                            "product-fixture-panda-base",
                        }
                        else time(8)
                    ),
                    tzinfo=CHINA_TIMEZONE,
                ),
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

    async def get_revision_route(
        self,
        request: TripRequest,
        route_request: RouteRequest,
        data_mode: DataMode,
    ) -> RouteLeg:
        del request
        if data_mode != DataMode.FIXTURE:
            raise ValueError("fixture revision route requires fixture data mode")
        return await self._provider.get_route(route_request)
