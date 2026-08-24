import httpx

from app.core.config import Settings
from app.domain.destination import CityResolutionCandidate, DestinationResolution
from app.domain.sources import DataMode
from app.providers.city_resolver import AmapCityResolverProvider, FixtureCityResolverProvider


class DestinationResolutionConfigurationError(RuntimeError):
    pass


class DestinationSelectionError(RuntimeError):
    def __init__(self, error_code: str, user_message: str) -> None:
        super().__init__(user_message)
        self.error_code = error_code
        self.user_message = user_message


class DestinationResolutionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fixture = FixtureCityResolverProvider()

    async def resolve(self, input_name: str, *, data_mode: DataMode) -> DestinationResolution:
        if data_mode == DataMode.FIXTURE:
            return await self._fixture.resolve_destination(input_name)
        if data_mode != DataMode.LIVE:
            raise DestinationResolutionConfigurationError(
                "destination resolution only supports fixture or live data modes"
            )
        if self._settings.amap_maps_api_key is None:
            raise DestinationResolutionConfigurationError(
                "live destination resolution requires AMAP_MAPS_API_KEY"
            )
        async with httpx.AsyncClient(timeout=self._settings.amap_mcp_timeout_seconds) as client:
            provider = AmapCityResolverProvider(self._settings, client)
            return await provider.resolve_destination(input_name)

    async def resolve_and_select(
        self,
        input_name: str,
        *,
        data_mode: DataMode,
        selected_adcode: str | None,
    ) -> CityResolutionCandidate:
        resolution = await self.resolve(input_name, data_mode=data_mode)
        try:
            return resolution.select(selected_adcode)
        except ValueError as error:
            if resolution.status.value == "unsupported":
                code = "destination-fixture-unsupported"
                message = "Fixture 模式仅覆盖北京、上海和成都; 其他城市请选择实时 Provider。"
            elif resolution.status.value == "no_result":
                code = "destination-not-found"
                message = "没有找到可用于规划的国内城市, 请补充省份或检查名称。"
            elif resolution.status.value == "ambiguous" and selected_adcode is None:
                code = "destination-ambiguous"
                message = "目的地名称存在多个行政区结果, 请先选择正确城市。"
            else:
                code = "destination-selection-invalid"
                message = "所选城市与服务端解析结果不一致, 请重新解析。"
            raise DestinationSelectionError(code, message) from error
