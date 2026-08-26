from decimal import ROUND_HALF_UP, Decimal

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
from app.planning.material_contracts import PlanningShortlist

MONEY_QUANTUM = Decimal("0.01")

# These deliberately broad, versioned ranges are planning references rather
# than live quotes. They are independent from the user's budget target.
REFERENCE_UNIT_RANGES: dict[BudgetCategory, MoneyRange] = {
    BudgetCategory.LODGING: MoneyRange(minimum=Decimal("300"), maximum=Decimal("650")),
    BudgetCategory.TRANSPORT: MoneyRange(minimum=Decimal("20"), maximum=Decimal("80")),
    BudgetCategory.FOOD: MoneyRange(minimum=Decimal("100"), maximum=Decimal("200")),
    BudgetCategory.ADMISSION: MoneyRange(minimum=Decimal("0"), maximum=Decimal("120")),
    BudgetCategory.ACTIVITY: MoneyRange(minimum=Decimal("0"), maximum=Decimal("80")),
    BudgetCategory.OTHER: MoneyRange(minimum=Decimal("50"), maximum=Decimal("150")),
}

DEFAULT_ESTIMATE_SCOPE = (
    BudgetCategory.LODGING,
    BudgetCategory.TRANSPORT,
    BudgetCategory.FOOD,
    BudgetCategory.ADMISSION,
    BudgetCategory.ACTIVITY,
)


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


def _quantity(
    category: BudgetCategory,
    context: PlannerContext,
    shortlist: PlanningShortlist,
) -> tuple[BudgetEstimateQuantityBasis, Decimal] | None:
    travelers = Decimal(context.party.total_travelers)
    days = Decimal(context.day_count)
    if category == BudgetCategory.LODGING:
        if context.party.room_nights is None:
            return None
        return BudgetEstimateQuantityBasis.ROOM_NIGHT, Decimal(context.party.room_nights)
    if category in {BudgetCategory.TRANSPORT, BudgetCategory.FOOD, BudgetCategory.ACTIVITY}:
        return BudgetEstimateQuantityBasis.TRAVELER_DAY, travelers * days
    if category == BudgetCategory.ADMISSION:
        activity_count = max(len(shortlist.poi_candidates), context.day_count)
        return BudgetEstimateQuantityBasis.TRAVELER_ACTIVITY, travelers * Decimal(activity_count)
    return BudgetEstimateQuantityBasis.PARTY_TRIP, Decimal("1")


def _description(category: BudgetCategory) -> str:
    return {
        BudgetCategory.LODGING: "住宿",
        BudgetCategory.TRANSPORT: "市内交通",
        BudgetCategory.FOOD: "日常餐饮",
        BudgetCategory.ADMISSION: "景点门票",
        BudgetCategory.ACTIVITY: "额外体验",
        BudgetCategory.OTHER: "机动费用",
    }[category]


def _basis_description(
    category: BudgetCategory,
    quantity: Decimal,
    *,
    uses_candidate_price: bool,
) -> str:
    count = int(quantity)
    if category == BudgetCategory.LODGING:
        source = "住宿候选价格区间" if uses_candidate_price else "国内城市住宿参考区间"
        return f"{count} 间夜 · {source}"
    if category == BudgetCategory.TRANSPORT:
        return f"{count} 人天 · 按市内公共交通和短途打车混合估算"
    if category == BudgetCategory.FOOD:
        return f"{count} 人天 · 按三餐日常消费区间"
    if category == BudgetCategory.ADMISSION:
        return f"{count} 人次 · 按当前主要活动数量估算"
    if category == BudgetCategory.ACTIVITY:
        return f"{count} 人天 · 预留可选体验费用"
    return "整趟行程预留"


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
) -> BudgetEstimate:
    scope = _estimate_scope(context)
    items: list[BudgetEstimateItem] = []
    unknown: list[BudgetCategory] = []
    for category in scope:
        quantity_result = _quantity(category, context, shortlist)
        if quantity_result is None:
            unknown.append(category)
            continue
        quantity_basis, quantity = quantity_result
        candidate_price = (
            shortlist.primary_stay.nightly_price_estimate
            if category == BudgetCategory.LODGING and shortlist.primary_stay is not None
            else None
        )
        uses_candidate_price = candidate_price is not None
        unit_price = candidate_price or REFERENCE_UNIT_RANGES[category]
        assert unit_price is not None
        items.append(
            BudgetEstimateItem(
                category=category,
                description=_description(category),
                quantity_basis=quantity_basis,
                quantity=quantity,
                unit_price=unit_price,
                total=_money_range(
                    quantity * unit_price.minimum,
                    quantity * unit_price.maximum,
                ),
                method=(
                    BudgetEstimateMethod.CANDIDATE_PRICE_RANGE
                    if uses_candidate_price
                    else BudgetEstimateMethod.PLANNING_REFERENCE
                ),
                confidence=(
                    BudgetEstimateConfidence.MEDIUM
                    if uses_candidate_price
                    else BudgetEstimateConfidence.LOW
                ),
                basis_description=_basis_description(
                    category,
                    quantity,
                    uses_candidate_price=uses_candidate_price,
                ),
            )
        )

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
