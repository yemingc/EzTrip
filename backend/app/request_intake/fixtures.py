import re
from datetime import date

from app.agents.contracts import (
    ConstraintEvidenceMode,
    ConstraintModelResponse,
    ConstraintProposalBatch,
    ConstraintProposalItem,
)
from app.domain.request import ConstraintKind, ConstraintStrength
from app.request_intake.contracts import (
    RequestEvidenceMode,
    RequestFieldName,
    RequestFieldProposalBatch,
    RequestFieldProposalItem,
    RequestIntakeModelResponse,
)

CHINESE_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    return CHINESE_DIGITS.get(text)


def _chinese_amount(text: str) -> int | None:
    compact = text.replace(",", "")
    if compact.isdigit():
        return int(compact)
    match = re.fullmatch(r"([一二两三四五六七八九])千(?:([一二三四五六七八九])百)?", compact)
    if not match:
        return None
    thousands = {**CHINESE_DIGITS, "六": 6, "七": 7, "八": 8, "九": 9}[match.group(1)]
    hundreds = 0
    if match.group(2):
        hundreds = {**CHINESE_DIGITS, "六": 6, "七": 7, "八": 8, "九": 9}[match.group(2)]
    return thousands * 1000 + hundreds * 100


class FixtureRequestFieldProposalModel:
    """Bounded parser for fixture/E2E scenarios; it is not a general Chinese NLU claim."""

    def propose(self, raw_text: str, reference_date: date) -> RequestIntakeModelResponse:
        del reference_date
        items: list[RequestFieldProposalItem] = []

        origin = re.search(r"从([\u4e00-\u9fff]{2,8}?)(?:出发|去)", raw_text)
        if origin:
            items.append(self._item("origin_city", origin.group(1), origin.group(0)))
        destination = re.search(r"(?:去|到)([\u4e00-\u9fff]{2,8}?)(?:玩|旅行|旅游|看|待)", raw_text)
        if destination:
            items.append(self._item("destination_city", destination.group(1), destination.group(0)))

        exact_date = re.search(r"20\d{2}-\d{2}-\d{2}", raw_text)
        if exact_date:
            items.append(self._item("start_date", exact_date.group(0), exact_date.group(0)))
        else:
            ambiguous_date = re.search(
                r"(?:下个月|[一二三四五六七八九十]{1,3}月)"
                r"(?![一二三四五六七八九十\d]{1,3}日)",
                raw_text,
            )
            if ambiguous_date:
                items.append(
                    self._item("start_date", ambiguous_date.group(0), ambiguous_date.group(0))
                )

        duration = re.search(r"([二两三四五2-5])(?:天|日)", raw_text)
        if duration and (value := _number(duration.group(1))) is not None:
            items.append(self._item("trip_days", str(value), duration.group(0)))

        if "和父母" in raw_text:
            items.append(self._item("adults", "3", "和父母", RequestEvidenceMode.INFERRED))
        else:
            adults = re.search(r"([一二两三四五1-5])(?:位|个)?成年人", raw_text)
            if adults and (value := _number(adults.group(1))) is not None:
                items.append(self._item("adults", str(value), adults.group(0)))
            elif "带一个孩子" in raw_text:
                items.append(self._item("adults", "1", "带一个孩子", RequestEvidenceMode.INFERRED))
        children = re.search(r"带([一二两三1-3])个孩子", raw_text)
        if children and (value := _number(children.group(1))) is not None:
            items.append(self._item("children", str(value), children.group(0)))
        seniors = re.search(r"([一二两三1-3])(?:位|个)老人", raw_text)
        if seniors and (value := _number(seniors.group(1))) is not None:
            items.append(self._item("seniors", str(value), seniors.group(0)))

        budget = re.search(
            r"预算\s*([\d,]+|[一二两三四五六七八九]千(?:[一二三四五六七八九]百)?)",
            raw_text,
        )
        if budget and (value := _chinese_amount(budget.group(1))) is not None:
            items.append(self._item("budget_limit", str(value), budget.group(0)))
        if "轻松" in raw_text or "慢节奏" in raw_text:
            evidence = "轻松" if "轻松" in raw_text else "慢节奏"
            items.append(self._item("pace", "relaxed", evidence))
        elif "紧凑" in raw_text or "标准节奏" in raw_text:
            evidence = "紧凑" if "紧凑" in raw_text else "标准节奏"
            items.append(self._item("pace", "standard", evidence))

        for phrase, style in (
            ("历史文化", "历史文化"),
            ("古建筑", "古建筑"),
            ("科技馆", "科技"),
            ("亲子", "亲子"),
            ("闽南美食", "闽南美食"),
        ):
            if phrase in raw_text:
                items.append(self._item("travel_style", style, phrase))
        return RequestIntakeModelResponse(
            proposal=RequestFieldProposalBatch(items=tuple(items)),
            model="fixture-request-intake-v1",
            latency_ms=0,
        )

    @staticmethod
    def _item(
        field: RequestFieldName | str,
        value: str,
        evidence: str,
        evidence_mode: RequestEvidenceMode = RequestEvidenceMode.EXPLICIT,
    ) -> RequestFieldProposalItem:
        return RequestFieldProposalItem(
            field=field,
            value=value,
            evidence=evidence,
            evidence_mode=evidence_mode,
        )


class FixtureConstraintProposalModel:
    def propose(self, raw_text: str) -> ConstraintModelResponse:
        items: list[ConstraintProposalItem] = []
        avoid = re.search(r"不要([^\uFF0C\u3002\uFF1B,;]{1,16})", raw_text)
        if avoid:
            items.append(
                self._item(
                    ConstraintKind.AVOID,
                    avoid.group(1),
                    ConstraintStrength.HARD,
                    5,
                    avoid.group(0),
                )
            )
        must = re.search(r"必须去([^\uFF0C\u3002\uFF1B,;]{1,20})", raw_text)
        if must:
            for value in re.split(r"和|、", must.group(1)):
                items.append(
                    self._item(
                        ConstraintKind.MUST_VISIT,
                        value,
                        ConstraintStrength.HARD,
                        5,
                        must.group(0),
                    )
                )
        walking = next(
            (item for item in ("尽量少走路", "少走路", "轻步行") if item in raw_text),
            None,
        )
        if walking:
            items.append(
                self._item(
                    ConstraintKind.WALKING_INTENSITY,
                    "low",
                    ConstraintStrength.SOFT,
                    4,
                    walking,
                )
            )
        for phrase in ("历史文化", "古建筑", "科技馆", "亲子活动"):
            if phrase in raw_text:
                items.append(
                    self._item(
                        ConstraintKind.INTEREST,
                        phrase,
                        ConstraintStrength.SOFT,
                        3,
                        phrase,
                    )
                )
        if "闽南美食" in raw_text:
            items.append(
                self._item(
                    ConstraintKind.MEAL,
                    "闽南美食",
                    ConstraintStrength.SOFT,
                    3,
                    "闽南美食",
                )
            )
        return ConstraintModelResponse(
            proposal=ConstraintProposalBatch(items=tuple(items)),
            model="fixture-constraint-intake-v1",
            latency_ms=0,
        )

    @staticmethod
    def _item(
        kind: ConstraintKind,
        value: str,
        strength: ConstraintStrength,
        priority: int,
        evidence: str,
    ) -> ConstraintProposalItem:
        return ConstraintProposalItem(
            kind=kind,
            value=value,
            strength=strength,
            priority=priority,
            evidence=evidence,
            evidence_mode=ConstraintEvidenceMode.EXPLICIT,
        )
