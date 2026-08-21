from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ExploreAgentResult, ModelTokenUsage, StayAgentResult
from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.context import PlannerContext
from app.domain.provider import ProviderErrorCategory
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.travel_data import WeatherRisk


class SpecialistName(StrEnum):
    EXPLORE = "explore"
    STAY = "stay"
    WEATHER = "weather"


class SpecialistBranchStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class SpecialistFanoutStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class SpecialistSkipReason(StrEnum):
    CAPABILITY_BLOCKED = "capability_blocked"


class SpecialistFailureCategory(StrEnum):
    PROVIDER = "provider"
    PROTOCOL = "protocol"
    DEPENDENCY = "dependency"


class SpecialistFailure(DomainModel):
    specialist: SpecialistName
    category: SpecialistFailureCategory
    error_code: Identifier
    retryable: bool
    provider_category: ProviderErrorCategory | None = None

    @model_validator(mode="after")
    def validate_failure_category(self) -> "SpecialistFailure":
        if (self.category == SpecialistFailureCategory.PROVIDER) != (
            self.provider_category is not None
        ):
            raise ValueError("only provider specialist failures carry a provider category")
        if self.category != SpecialistFailureCategory.PROVIDER and self.retryable:
            raise ValueError("only typed provider failures may be marked retryable")
        return self


class SpecialistBranchResult(DomainModel):
    specialist: SpecialistName
    status: SpecialistBranchStatus
    elapsed_ms: int = Field(ge=0)
    model_call_count: int = Field(ge=0, le=2)
    provider_call_count: int = Field(ge=0, le=4)
    model_usages: tuple[ModelTokenUsage, ...] = Field(default=(), max_length=2)
    explore_result: ExploreAgentResult | None = None
    stay_result: StayAgentResult | None = None
    weather_risks: tuple[WeatherRisk, ...] = ()
    skip_reason: SpecialistSkipReason | None = None
    failure: SpecialistFailure | None = None

    @model_validator(mode="after")
    def validate_branch_payload(self) -> "SpecialistBranchResult":
        if self.failure is not None and self.failure.specialist != self.specialist:
            raise ValueError("specialist failure must match its branch")
        if self.status == SpecialistBranchStatus.SKIPPED:
            if (
                any(
                    (
                        self.explore_result,
                        self.stay_result,
                        self.weather_risks,
                        self.failure,
                        self.model_call_count,
                        self.provider_call_count,
                        self.model_usages,
                    )
                )
                or self.skip_reason is None
            ):
                raise ValueError("skipped specialist branches cannot contain calls or results")
            return self
        if self.skip_reason is not None:
            raise ValueError("only skipped specialist branches carry a skip reason")
        if len(self.model_usages) > self.model_call_count:
            raise ValueError("specialist usage records cannot exceed model calls")
        if self.status == SpecialistBranchStatus.FAILED:
            if self.failure is None or any(
                (self.explore_result, self.stay_result, self.weather_risks)
            ):
                raise ValueError("failed specialist branches require only typed failure data")
            if self.specialist == SpecialistName.WEATHER and (
                self.model_call_count or self.model_usages
            ):
                raise ValueError("weather specialist cannot call a model")
            return self
        if self.failure is not None:
            raise ValueError("successful specialist branches cannot contain a failure")
        if self.specialist == SpecialistName.EXPLORE:
            if (
                self.explore_result is None
                or self.stay_result is not None
                or self.weather_risks
                or self.model_call_count != 2
                or self.provider_call_count < 1
            ):
                raise ValueError("successful Explore branch has an invalid payload")
            expected_usages = tuple(
                usage
                for usage in (
                    self.explore_result.query_usage,
                    self.explore_result.selection_usage,
                )
                if usage is not None
            )
            if self.model_usages != expected_usages:
                raise ValueError("Explore branch usage must match its Agent result")
        elif self.specialist == SpecialistName.STAY:
            if (
                self.stay_result is None
                or self.explore_result is not None
                or self.weather_risks
                or self.model_call_count != 2
                or self.provider_call_count < 1
            ):
                raise ValueError("successful Stay branch has an invalid payload")
            expected_usages = tuple(
                usage
                for usage in (
                    self.stay_result.query_usage,
                    self.stay_result.selection_usage,
                )
                if usage is not None
            )
            if self.model_usages != expected_usages:
                raise ValueError("Stay branch usage must match its Agent result")
        elif (
            self.explore_result is not None
            or self.stay_result is not None
            or self.model_call_count
            or self.model_usages
            or self.provider_call_count != 1
        ):
            raise ValueError("successful Weather branch has an invalid payload")
        return self


class SpecialistFanoutResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["specialist-fanout-v1"] = "specialist-fanout-v1"
    request_id: Identifier
    context_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    status: SpecialistFanoutStatus
    planner_context: PlannerContext
    branches: tuple[SpecialistBranchResult, ...] = Field(min_length=3, max_length=3)
    total_model_call_count: int = Field(ge=0, le=4)
    total_provider_call_count: int = Field(ge=0, le=9)
    fanout_latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_fanout_merge(self) -> "SpecialistFanoutResult":
        if (
            self.request_id != self.planner_context.request_id
            or self.context_id != self.planner_context.context_id
        ):
            raise ValueError("fan-out result must preserve PlannerContext identity")
        if tuple(item.specialist for item in self.branches) != tuple(SpecialistName):
            raise ValueError("fan-out merge requires one ordered result per specialist")
        successful = sum(item.status == SpecialistBranchStatus.SUCCEEDED for item in self.branches)
        failed = sum(item.status == SpecialistBranchStatus.FAILED for item in self.branches)
        expected_status = SpecialistFanoutStatus.PARTIAL
        if successful == len(self.branches):
            expected_status = SpecialistFanoutStatus.COMPLETE
        elif successful == 0 and failed == 0:
            expected_status = SpecialistFanoutStatus.BLOCKED
        elif successful == 0:
            expected_status = SpecialistFanoutStatus.FAILED
        if self.status != expected_status:
            raise ValueError("fan-out status must match specialist branch outcomes")
        expected_model_calls = sum(item.model_call_count for item in self.branches)
        expected_provider_calls = sum(item.provider_call_count for item in self.branches)
        if self.total_model_call_count != expected_model_calls:
            raise ValueError("fan-out model calls must match branch calls")
        if self.total_provider_call_count != expected_provider_calls:
            raise ValueError("fan-out provider calls must match branch calls")
        if self.fanout_latency_ms < max(item.elapsed_ms for item in self.branches):
            raise ValueError("fan-out latency cannot be below its slowest branch")
        for branch in self.branches:
            if branch.explore_result is not None and (
                branch.explore_result.request_id != self.request_id
                or branch.explore_result.context_id != self.context_id
            ):
                raise ValueError("Explore result must preserve fan-out identity")
            if branch.stay_result is not None and (
                branch.stay_result.request_id != self.request_id
                or branch.stay_result.context_id != self.context_id
            ):
                raise ValueError("Stay result must preserve fan-out identity")
        weather = next(item for item in self.branches if item.specialist == SpecialistName.WEATHER)
        weather_ids = [item.risk_id for item in weather.weather_risks]
        if len(weather_ids) != len(set(weather_ids)):
            raise ValueError("weather branch cannot contain duplicate risk ids")
        if any(
            item.city != self.planner_context.destination.normalized_name
            or item.source.data_mode != self.data_mode
            for item in weather.weather_risks
        ):
            raise ValueError("weather risks must match fan-out city and data mode")
        return self


class SpecialistFanoutSnapshot(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    thread_id: Identifier
    checkpoint_id: NonEmptyText
    next_nodes: tuple[NonEmptyText, ...]
    request: TripRequest
    result: SpecialistFanoutResult

    @model_validator(mode="after")
    def validate_completed_snapshot(self) -> "SpecialistFanoutSnapshot":
        if self.next_nodes:
            raise ValueError("completed specialist fan-out snapshots cannot have next nodes")
        if self.request.request_id != self.result.request_id:
            raise ValueError("snapshot request must match the specialist fan-out result")
        return self
