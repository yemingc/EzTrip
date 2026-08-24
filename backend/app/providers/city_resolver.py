import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.domain.destination import (
    AdministrativeLevel,
    CityResolutionCandidate,
    DestinationResolution,
    DestinationResolutionStatus,
)
from app.domain.provider import ProviderErrorCategory
from app.domain.sources import DataMode, SourceReference
from app.providers.amap_protocol import build_amap_failure, classify_amap_infocode
from app.providers.errors import ProviderRequestError

FIXTURE_RETRIEVED_AT = datetime(2026, 8, 24, tzinfo=UTC)
MUNICIPALITIES = frozenset({"北京市", "上海市", "天津市", "重庆市"})


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source(
    *,
    provider: str,
    provider_id: str,
    data_mode: DataMode,
    retrieved_at: datetime,
    raw_hash: str,
) -> SourceReference:
    return SourceReference(
        provider=provider,
        provider_id=provider_id,
        data_mode=data_mode,
        retrieved_at=retrieved_at,
        raw_response_sha256=raw_hash,
    )


def _fixture_candidate(
    *,
    candidate_id: str,
    qualified_name: str,
    planning_city_name: str,
    administrative_code: str,
    level: AdministrativeLevel,
    province_name: str,
    city_name: str | None = None,
    district_name: str | None = None,
) -> CityResolutionCandidate:
    payload = {
        "qualified_name": qualified_name,
        "planning_city_name": planning_city_name,
        "administrative_code": administrative_code,
        "level": level.value,
    }
    return CityResolutionCandidate(
        candidate_id=candidate_id,
        qualified_name=qualified_name,
        planning_city_name=planning_city_name,
        administrative_code=administrative_code,
        level=level,
        province_name=province_name,
        city_name=city_name,
        district_name=district_name,
        source=_source(
            provider="eztrip-city-fixture",
            provider_id=administrative_code,
            data_mode=DataMode.FIXTURE,
            retrieved_at=FIXTURE_RETRIEVED_AT,
            raw_hash=_sha256(payload),
        ),
    )


BEIJING = _fixture_candidate(
    candidate_id="fixture-city-beijing",
    qualified_name="北京市",
    planning_city_name="北京市",
    administrative_code="110000",
    level=AdministrativeLevel.PROVINCE,
    province_name="北京市",
)
SHANGHAI = _fixture_candidate(
    candidate_id="fixture-city-shanghai",
    qualified_name="上海市",
    planning_city_name="上海市",
    administrative_code="310000",
    level=AdministrativeLevel.PROVINCE,
    province_name="上海市",
)
CHENGDU = _fixture_candidate(
    candidate_id="fixture-city-chengdu",
    qualified_name="四川省成都市",
    planning_city_name="成都市",
    administrative_code="510100",
    level=AdministrativeLevel.CITY,
    province_name="四川省",
    city_name="成都市",
)
BEIJING_CHAOYANG = _fixture_candidate(
    candidate_id="fixture-district-beijing-chaoyang",
    qualified_name="北京市朝阳区",
    planning_city_name="北京市",
    administrative_code="110105",
    level=AdministrativeLevel.DISTRICT,
    province_name="北京市",
    district_name="朝阳区",
)
LIAONING_CHAOYANG = _fixture_candidate(
    candidate_id="fixture-city-liaoning-chaoyang",
    qualified_name="辽宁省朝阳市",
    planning_city_name="朝阳市",
    administrative_code="211300",
    level=AdministrativeLevel.CITY,
    province_name="辽宁省",
    city_name="朝阳市",
)

FIXTURE_ALIASES: dict[str, tuple[CityResolutionCandidate, ...]] = {
    "北京": (BEIJING,),
    "北京市": (BEIJING,),
    "上海": (SHANGHAI,),
    "上海市": (SHANGHAI,),
    "成都": (CHENGDU,),
    "成都市": (CHENGDU,),
    "朝阳": (BEIJING_CHAOYANG, LIAONING_CHAOYANG),
}


class FixtureCityResolverProvider:
    async def resolve_destination(self, input_name: str) -> DestinationResolution:
        query = input_name.strip()
        candidates = FIXTURE_ALIASES.get(query, ())
        if not candidates:
            return DestinationResolution(
                input_name=query,
                data_mode=DataMode.FIXTURE,
                status=DestinationResolutionStatus.UNSUPPORTED,
            )
        return DestinationResolution(
            input_name=query,
            data_mode=DataMode.FIXTURE,
            status=(
                DestinationResolutionStatus.RESOLVED
                if len(candidates) == 1
                else DestinationResolutionStatus.AMBIGUOUS
            ),
            candidates=candidates,
        )


def _text(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _level(value: object) -> AdministrativeLevel | None:
    return {
        "省": AdministrativeLevel.PROVINCE,
        "市": AdministrativeLevel.CITY,
        "区县": AdministrativeLevel.DISTRICT,
    }.get(_text(value) or "")


def _qualified_name(
    *,
    province: str | None,
    city: str | None,
    district: str | None,
    formatted_address: str | None,
) -> str | None:
    parts: list[str] = []
    for value in (province, city, district):
        if value and value not in parts:
            parts.append(value)
    if parts:
        return "".join(parts)
    return formatted_address


class AmapCityResolverProvider:
    """Resolve domestic administrative destinations through AMap REST geocoding."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock

    async def resolve_destination(self, input_name: str) -> DestinationResolution:
        query = input_name.strip()
        secret = self._settings.amap_maps_api_key
        if secret is None:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="resolve_destination",
                    category=ProviderErrorCategory.AUTHENTICATION_FAILED,
                    message="AMAP_MAPS_API_KEY is required for live destination resolution",
                )
            )
        try:
            response = await self._client.get(
                self._settings.amap_rest_geocode_url,
                params={
                    "key": secret.get_secret_value(),
                    "address": query,
                    "output": "JSON",
                },
            )
            if response.status_code == 429:
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="resolve_destination",
                        category=ProviderErrorCategory.RATE_LIMITED,
                        message="AMap destination resolver returned HTTP 429",
                    )
                )
            if response.status_code >= 400:
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="resolve_destination",
                        category=ProviderErrorCategory.UNRECOVERABLE,
                        message=f"AMap destination resolver returned HTTP {response.status_code}",
                    )
                )
            decoded = response.json()
        except ProviderRequestError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="resolve_destination",
                    category=ProviderErrorCategory.TIMEOUT,
                    message="AMap destination resolver timed out",
                )
            ) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="resolve_destination",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap destination resolver returned an invalid response",
                )
            ) from exc

        if not isinstance(decoded, Mapping):
            raise ProviderRequestError(
                build_amap_failure(
                    operation="resolve_destination",
                    category=ProviderErrorCategory.MISSING_FIELD,
                    message="AMap destination resolver did not return an object",
                )
            )
        payload = {str(key): value for key, value in decoded.items()}
        infocode = str(payload.get("infocode", ""))
        if payload.get("status") != "1" or infocode != "10000":
            raise ProviderRequestError(
                build_amap_failure(
                    operation="resolve_destination",
                    category=classify_amap_infocode(infocode),
                    message=f"AMap destination resolver failed with infocode {infocode}",
                )
            )

        geocodes = payload.get("geocodes")
        if not isinstance(geocodes, Sequence) or isinstance(geocodes, (str, bytes)):
            raise ProviderRequestError(
                build_amap_failure(
                    operation="resolve_destination",
                    category=ProviderErrorCategory.MISSING_FIELD,
                    message="AMap destination resolver response has no geocodes list",
                )
            )

        retrieved_at = self._clock()
        raw_hash = _sha256(payload)
        candidates_by_code: dict[str, CityResolutionCandidate] = {}
        for item in geocodes:
            if not isinstance(item, Mapping):
                continue
            administrative_code = _text(item.get("adcode"))
            level = _level(item.get("level"))
            if administrative_code is None or level is None:
                continue
            if len(administrative_code) != 6 or not administrative_code.isdigit():
                continue
            province = _text(item.get("province"))
            city = _text(item.get("city"))
            district = _text(item.get("district"))
            formatted = _text(item.get("formatted_address"))
            qualified = _qualified_name(
                province=province,
                city=city,
                district=district,
                formatted_address=formatted,
            )
            planning_city = city or (province if province in MUNICIPALITIES else None)
            if planning_city is None and level == AdministrativeLevel.CITY:
                planning_city = formatted or query
            if qualified is None or planning_city is None:
                continue
            candidate = CityResolutionCandidate(
                candidate_id=f"amap-city-{administrative_code}",
                qualified_name=qualified,
                planning_city_name=planning_city,
                administrative_code=administrative_code,
                level=level,
                province_name=province,
                city_name=city,
                district_name=district,
                center=_text(item.get("location")),
                source=_source(
                    provider="amap-geocode-rest",
                    provider_id=administrative_code,
                    data_mode=DataMode.LIVE,
                    retrieved_at=retrieved_at,
                    raw_hash=raw_hash,
                ),
            )
            candidates_by_code.setdefault(administrative_code, candidate)

        candidates = tuple(candidates_by_code.values())
        if not candidates:
            status = DestinationResolutionStatus.NO_RESULT
        elif len(candidates) == 1:
            status = DestinationResolutionStatus.RESOLVED
        else:
            status = DestinationResolutionStatus.AMBIGUOUS
        return DestinationResolution(
            input_name=query,
            data_mode=DataMode.LIVE,
            status=status,
            candidates=candidates,
        )
