from decimal import ROUND_HALF_UP, Decimal

from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.money import BudgetCategory, MoneyRange
from app.planning.budget_estimate_contracts import (
    BudgetAdviceCode,
    BudgetComparisonStatus,
    BudgetEstimate,
    BudgetEstimateConfidence,
    BudgetEstimateItem,
    BudgetEstimateMethod,
    BudgetEstimateQuantityBasis,
    BudgetEstimateStatus,
)
from app.planning.material_contracts import PlanningShortlist, RouteMatrix

MONEY_QUANTUM = Decimal("0.01")
MEALS_PER_TRAVELER_DAY = Decimal("3")

# Versioned independent-trip references. They are separate from the user's
# target budget and are never presented as quotes or booking prices.
REFERENCE_UNIT_RANGES: dict[BudgetCategory, MoneyRange] = {
    BudgetCategory.LODGING: MoneyRange(minimum=Decimal("280"), maximum=Decimal("600")),
    BudgetCategory.TRANSPORT: MoneyRange(minimum=Decimal("20"), maximum=Decimal("70")),
    BudgetCategory.FOOD: MoneyRange(minimum=Decimal("30"), maximum=Decimal("65")),
    BudgetCategory.ADMISSION: MoneyRange(minimum=Decimal("0"), maximum=Decimal("120")),
    BudgetCategory.ACTIVITY: MoneyRange(minimum=Decimal("0"), maximum=Decimal("80")),
    BudgetCategory.OTHER: MoneyRange(minimum=Decimal("50"), maximum=Decimal("150")),
}
ROUTE_LEG_REFERENCE = MoneyRange(minimum=Decimal("5"), maximum=Decimal("25"))

DEFAULT_ESTIMATE_SCOPE = (
    BudgetCategory.LODGING,
    BudgetCategory.TRANSPORT,
    BudgetCategory.FOOD,
    BudgetCategory.ADMISSION,
)

ADMISSION_REFERENCE_RULES: tuple[tuple[tuple[str, ...], MoneyRange], ...] = (
    (("主题公园", "游乐园", "乐园"), MoneyRange(minimum=Decimal("120"), maximum=Decimal("450"))),
    (("观景台", "电视塔", "摩天轮"), MoneyRange(minimum=Decimal("80"), maximum=Decimal("250"))),
    (("动物园", "海洋馆", "水族馆"), MoneyRange(minimum=Decimal("20"), maximum=Decimal("160"))),
    (
        ("博物馆", "博物院", "美术馆", "艺术馆"),
        MoneyRange(minimum=Decimal("0"), maximum=Decimal("60")),
    ),
    (
        ("公园", "园林", "古镇", "寺", "祠", "故居"),
        MoneyRange(minimum=Decimal("0"), maximum=Decimal("80")),
    ),
    (
        ("街区", "步行街", "外滩", "滨水", "广场"),
        MoneyRange(minimum=Decimal("0"), maximum=Decimal("20")),
    ),
)

# Stable planning references for the bundled city examples. These are not live
# quotes; unknown cities and places fall back to the category rules above.
KNOWN_ADMISSION_REFERENCE_RANGES: dict[str, MoneyRange] = {
    "故宫博物院": MoneyRange(minimum="60", maximum="60"),
    "天坛公园": MoneyRange(minimum="15", maximum="34"),
    "中国国家博物馆": MoneyRange(minimum="0", maximum="0"),
    "景山公园": MoneyRange(minimum="2", maximum="2"),
    "北海公园": MoneyRange(minimum="10", maximum="20"),
    "什刹海": MoneyRange(minimum="0", maximum="0"),
    "恭王府": MoneyRange(minimum="40", maximum="40"),
    "首都博物馆": MoneyRange(minimum="0", maximum="0"),
    "北京天文馆": MoneyRange(minimum="10", maximum="20"),
    "南锣鼓巷": MoneyRange(minimum="0", maximum="0"),
    "雍和宫": MoneyRange(minimum="25", maximum="25"),
    "颐和园": MoneyRange(minimum="30", maximum="30"),
    "圆明园遗址公园": MoneyRange(minimum="10", maximum="25"),
    "北京动物园": MoneyRange(minimum="15", maximum="20"),
    "奥林匹克公园": MoneyRange(minimum="0", maximum="0"),
    "798艺术区": MoneyRange(minimum="0", maximum="0"),
    "上海博物馆": MoneyRange(minimum="0", maximum="0"),
    "豫园": MoneyRange(minimum="30", maximum="40"),
    "外滩": MoneyRange(minimum="0", maximum="0"),
    "南京路步行街": MoneyRange(minimum="0", maximum="0"),
    "新天地": MoneyRange(minimum="0", maximum="0"),
    "田子坊": MoneyRange(minimum="0", maximum="0"),
    "武康路": MoneyRange(minimum="0", maximum="0"),
    "上海自然博物馆": MoneyRange(minimum="30", maximum="30"),
    "上海当代艺术博物馆": MoneyRange(minimum="0", maximum="0"),
    "中华艺术宫": MoneyRange(minimum="0", maximum="0"),
    "浦东美术馆": MoneyRange(minimum="80", maximum="120"),
    "东方明珠广播电视塔": MoneyRange(minimum="180", maximum="220"),
    "龙美术馆": MoneyRange(minimum="80", maximum="120"),
    "思南公馆": MoneyRange(minimum="0", maximum="0"),
    "北外滩": MoneyRange(minimum="0", maximum="0"),
    "金沙遗址博物馆": MoneyRange(minimum="70", maximum="70"),
    "成都大熊猫繁育研究基地": MoneyRange(minimum="55", maximum="55"),
    "成都博物馆": MoneyRange(minimum="0", maximum="0"),
    "人民公园": MoneyRange(minimum="0", maximum="0"),
    "宽窄巷子": MoneyRange(minimum="0", maximum="0"),
    "杜甫草堂": MoneyRange(minimum="50", maximum="50"),
    "四川博物院": MoneyRange(minimum="0", maximum="0"),
    "武侯祠": MoneyRange(minimum="50", maximum="50"),
    "锦里古街": MoneyRange(minimum="0", maximum="0"),
    "望江楼公园": MoneyRange(minimum="0", maximum="20"),
    "东郊记忆": MoneyRange(minimum="0", maximum="0"),
    "天府艺术公园": MoneyRange(minimum="0", maximum="0"),
    "九眼桥": MoneyRange(minimum="0", maximum="0"),
    "浣花溪公园": MoneyRange(minimum="0", maximum="0"),
    "太古里": MoneyRange(minimum="0", maximum="0"),
}


def _money_range(minimum: Decimal, maximum: Decimal) -> MoneyRange:
    return MoneyRange(
        minimum=minimum.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP),
        maximum=maximum.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP),
    )


def _estimate_scope(context: PlannerContext) -> tuple[BudgetCategory, ...]:
    categories = (
        context.budget.included_categories if context.budget is not None else DEFAULT_ESTIMATE_SCOPE
    )
    included = set(categories)
    return tuple(category for category in BudgetCategory if category in included)


def _description(category: BudgetCategory) -> str:
    return {
        BudgetCategory.LODGING: "住宿",
        BudgetCategory.TRANSPORT: "市内交通",
        BudgetCategory.FOOD: "日常餐饮",
        BudgetCategory.ADMISSION: "景点门票",
        BudgetCategory.ACTIVITY: "额外体验",
        BudgetCategory.OTHER: "机动费用",
    }[category]


def _build_item(
    category: BudgetCategory,
    quantity_basis: BudgetEstimateQuantityBasis,
    quantity: Decimal,
    unit_price: MoneyRange,
    method: BudgetEstimateMethod,
    confidence: BudgetEstimateConfidence,
    basis_description: str,
) -> BudgetEstimateItem:
    return BudgetEstimateItem(
        category=category,
        description=_description(category),
        quantity_basis=quantity_basis,
        quantity=quantity,
        unit_price=unit_price,
        total=_money_range(
            quantity * unit_price.minimum,
            quantity * unit_price.maximum,
        ),
        method=method,
        confidence=confidence,
        basis_description=basis_description,
    )


def _lodging_item(
    context: PlannerContext,
    shortlist: PlanningShortlist,
) -> BudgetEstimateItem | None:
    if context.party.room_nights is None:
        return None
    quantity = Decimal(context.party.room_nights)
    stay = shortlist.primary_stay
    candidate_price = stay.nightly_price_estimate if stay is not None else None
    uses_candidate_price = candidate_price is not None
    unit_price = candidate_price or REFERENCE_UNIT_RANGES[BudgetCategory.LODGING]
    stay_name = stay.name if stay is not None else "当前住宿区域"
    return _build_item(
        BudgetCategory.LODGING,
        BudgetEstimateQuantityBasis.ROOM_NIGHT,
        quantity,
        unit_price,
        (
            BudgetEstimateMethod.CANDIDATE_PRICE_RANGE
            if uses_candidate_price
            else BudgetEstimateMethod.PLANNING_REFERENCE
        ),
        (BudgetEstimateConfidence.MEDIUM if uses_candidate_price else BudgetEstimateConfidence.LOW),
        f"{stay_name} · {int(quantity)} 间夜",
    )


def _transport_item(
    context: PlannerContext,
    shortlist: PlanningShortlist,
    route_matrix: RouteMatrix | None,
) -> BudgetEstimateItem:
    travelers = Decimal(context.party.total_travelers)
    if (
        route_matrix is not None
        and route_matrix.expected_edge_count > 0
        and route_matrix.succeeded_edge_count == route_matrix.expected_edge_count
    ):
        # The route graph covers stay -> daily activities and activity -> activity.
        # Add one return-to-stay leg per day for a whole-day reference.
        route_legs = Decimal(len(shortlist.poi_candidates) + context.day_count)
        quantity = travelers * route_legs
        return _build_item(
            BudgetCategory.TRANSPORT,
            BudgetEstimateQuantityBasis.TRAVELER_ROUTE_LEG,
            quantity,
            ROUTE_LEG_REFERENCE,
            BudgetEstimateMethod.ROUTE_REFERENCE,
            BudgetEstimateConfidence.MEDIUM,
            (
                f"当前 {len(shortlist.poi_candidates)} 个活动 · "
                f"按每人 {int(route_legs)} 段市内出行估算 (含每日返程)"
            ),
        )
    traveler_days = travelers * Decimal(context.day_count)
    return _build_item(
        BudgetCategory.TRANSPORT,
        BudgetEstimateQuantityBasis.TRAVELER_DAY,
        traveler_days,
        REFERENCE_UNIT_RANGES[BudgetCategory.TRANSPORT],
        BudgetEstimateMethod.PLANNING_REFERENCE,
        BudgetEstimateConfidence.LOW,
        f"{int(traveler_days)} 人天 · 路线不完整, 按市内交通日均区间估算",
    )


def _food_item(context: PlannerContext) -> BudgetEstimateItem:
    quantity = (
        Decimal(context.party.total_travelers) * Decimal(context.day_count) * MEALS_PER_TRAVELER_DAY
    )
    return _build_item(
        BudgetCategory.FOOD,
        BudgetEstimateQuantityBasis.TRAVELER_MEAL,
        quantity,
        REFERENCE_UNIT_RANGES[BudgetCategory.FOOD],
        BudgetEstimateMethod.PLANNING_REFERENCE,
        BudgetEstimateConfidence.LOW,
        f"{int(quantity)} 人餐 · 按每天三餐的常规消费估算",
    )


def _poi_admission_range(candidate: CandidatePOI) -> MoneyRange:
    known_range = KNOWN_ADMISSION_REFERENCE_RANGES.get(candidate.name)
    if known_range is not None:
        return known_range
    searchable = " ".join((candidate.name, *candidate.categories, *candidate.tags))
    for keywords, price_range in ADMISSION_REFERENCE_RULES:
        if any(keyword in searchable for keyword in keywords):
            return price_range
    return REFERENCE_UNIT_RANGES[BudgetCategory.ADMISSION]


def _short_names(candidates: tuple[CandidatePOI, ...]) -> str:
    visible = "、".join(candidate.name for candidate in candidates[:3])
    suffix = "等" if len(candidates) > 3 else ""
    return f"{visible}{suffix} · {len(candidates)} 个活动"


def _admission_item(
    context: PlannerContext,
    shortlist: PlanningShortlist,
) -> BudgetEstimateItem:
    ranges = tuple(_poi_admission_range(candidate) for candidate in shortlist.poi_candidates)
    per_traveler = _money_range(
        sum((item.minimum for item in ranges), start=Decimal("0")),
        sum((item.maximum for item in ranges), start=Decimal("0")),
    )
    travelers = Decimal(context.party.total_travelers)
    return _build_item(
        BudgetCategory.ADMISSION,
        BudgetEstimateQuantityBasis.TRAVELER_TRIP,
        travelers,
        per_traveler,
        BudgetEstimateMethod.ITINERARY_PRICE_RANGE,
        BudgetEstimateConfidence.LOW,
        f"{_short_names(shortlist.poi_candidates)} · {int(travelers)} 人",
    )


def _generic_item(category: BudgetCategory, context: PlannerContext) -> BudgetEstimateItem:
    if category == BudgetCategory.ACTIVITY:
        quantity = Decimal(context.party.total_travelers * context.day_count)
        basis = BudgetEstimateQuantityBasis.TRAVELER_DAY
        description = f"{int(quantity)} 人天 · 仅计算额外体验预留"
    else:
        quantity = Decimal("1")
        basis = BudgetEstimateQuantityBasis.PARTY_TRIP
        description = "整趟行程预留"
    return _build_item(
        category,
        basis,
        quantity,
        REFERENCE_UNIT_RANGES[category],
        BudgetEstimateMethod.PLANNING_REFERENCE,
        BudgetEstimateConfidence.LOW,
        description,
    )


def _comparison(total: MoneyRange, budget_limit: Decimal | None) -> BudgetComparisonStatus:
    if budget_limit is None:
        return BudgetComparisonStatus.NOT_REQUESTED
    if total.minimum > budget_limit:
        return BudgetComparisonStatus.OVER_BUDGET
    if total.maximum > budget_limit:
        return BudgetComparisonStatus.POSSIBLE_OVERRUN
    return BudgetComparisonStatus.WITHIN_BUDGET


def _advice(
    comparison: BudgetComparisonStatus,
    scope: tuple[BudgetCategory, ...],
) -> tuple[BudgetAdviceCode, ...]:
    included = set(scope)
    if comparison == BudgetComparisonStatus.OVER_BUDGET:
        advice: list[BudgetAdviceCode] = []
        if BudgetCategory.LODGING in included:
            advice.append(BudgetAdviceCode.LOWER_LODGING_TIER)
        if included & {BudgetCategory.ADMISSION, BudgetCategory.ACTIVITY}:
            advice.append(BudgetAdviceCode.PRIORITIZE_FREE_ACTIVITIES)
        if BudgetCategory.TRANSPORT in included:
            advice.append(BudgetAdviceCode.USE_PUBLIC_TRANSPORT)
        return tuple(advice)
    if comparison == BudgetComparisonStatus.POSSIBLE_OVERRUN:
        advice = [BudgetAdviceCode.KEEP_BUFFER]
        if included & {BudgetCategory.ADMISSION, BudgetCategory.ACTIVITY}:
            advice.append(BudgetAdviceCode.PRIORITIZE_FREE_ACTIVITIES)
        return tuple(advice)
    if comparison == BudgetComparisonStatus.WITHIN_BUDGET:
        return (BudgetAdviceCode.KEEP_BUFFER,)
    return ()


def estimate_trip_budget(
    context: PlannerContext,
    shortlist: PlanningShortlist,
    route_matrix: RouteMatrix | None = None,
) -> BudgetEstimate:
    scope = _estimate_scope(context)
    items: list[BudgetEstimateItem] = []
    unknown: list[BudgetCategory] = []
    for category in scope:
        if category == BudgetCategory.LODGING:
            item = _lodging_item(context, shortlist)
            if item is None:
                unknown.append(category)
                continue
        elif category == BudgetCategory.TRANSPORT:
            item = _transport_item(context, shortlist, route_matrix)
        elif category == BudgetCategory.FOOD:
            item = _food_item(context)
        elif category == BudgetCategory.ADMISSION:
            item = _admission_item(context, shortlist)
        else:
            item = _generic_item(category, context)
        items.append(item)

    budget_limit = context.budget.total_limit if context.budget is not None else None
    if unknown:
        return BudgetEstimate(
            request_id=context.request_id,
            context_id=context.context_id,
            input_request_sha256=context.input_request_sha256,
            status=BudgetEstimateStatus.PARTIAL,
            scope_categories=scope,
            items=tuple(items),
            unknown_categories=tuple(unknown),
            budget_limit=budget_limit,
            comparison_status=BudgetComparisonStatus.INCOMPLETE,
            advice_codes=(),
        )

    total = _money_range(
        sum((item.total.minimum for item in items), start=Decimal("0")),
        sum((item.total.maximum for item in items), start=Decimal("0")),
    )
    comparison = _comparison(total, budget_limit)
    return BudgetEstimate(
        request_id=context.request_id,
        context_id=context.context_id,
        input_request_sha256=context.input_request_sha256,
        status=BudgetEstimateStatus.COMPLETE,
        scope_categories=scope,
        items=tuple(items),
        unknown_categories=(),
        total=total,
        per_traveler=_money_range(
            total.minimum / Decimal(context.party.total_travelers),
            total.maximum / Decimal(context.party.total_travelers),
        ),
        per_day=_money_range(
            total.minimum / Decimal(context.day_count),
            total.maximum / Decimal(context.day_count),
        ),
        budget_limit=budget_limit,
        comparison_status=comparison,
        advice_codes=_advice(comparison, scope),
    )
