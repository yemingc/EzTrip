import asyncio
import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.money import BudgetCategory
from app.request_intake import agent as agent_module
from app.request_intake.agent import (
    REQUEST_FIELD_TOOL_NAME,
    DeepSeekRequestFieldProposalModel,
    RequestIntakeProtocolError,
    normalize_request_field_response,
)
from app.request_intake.contracts import (
    RequestEvidenceMode,
    RequestFieldName,
    RequestFieldProposalBatch,
    RequestFieldProposalItem,
    RequestIntakeConfirmRequest,
    RequestIntakeCreateRequest,
    RequestIntakeModelResponse,
)
from app.request_intake.service import RequestIntakeService


def make_payload(
    raw_text: str = "和父母九月去泉州玩四天, 预算五千, 喜欢古建筑和闽南美食, 尽量少走路",
) -> RequestIntakeCreateRequest:
    return RequestIntakeCreateRequest(
        raw_text=raw_text,
        reference_date=date(2026, 8, 24),
        data_mode="fixture",
        form={
            "origin_city": "上海",
            "destination_city": "北京",
            "start_date": "2026-09-10",
            "trip_days": 2,
            "adults": 2,
            "children": 0,
            "seniors": 0,
            "rooms": 1,
            "budget_limit": "3000",
            "pace": "standard",
        },
    )


def test_fixture_intake_exposes_evidence_conflicts_and_ambiguous_date() -> None:
    async def exercise() -> None:
        service = RequestIntakeService(Settings(_env_file=None, environment="test"))
        draft = await service.propose(make_payload())

        assert draft.model_call_count == 0
        assert draft.proposed_fields.destination_city == "泉州"
        assert draft.proposed_fields.trip_days == 4
        assert draft.proposed_fields.adults == 3
        assert draft.proposed_fields.budget_limit == 5000
        assert draft.proposed_fields.travel_styles == ("古建筑", "闽南美食")
        statuses = {(item.field, item.evidence): item.status for item in draft.field_decisions}
        assert statuses[(RequestFieldName.DESTINATION_CITY, "去泉州玩")].value == "conflict"
        assert statuses[(RequestFieldName.START_DATE, "九月")].value == "needs_confirmation"
        assert any("start_date" in item for item in draft.clarifications)
        assert {item.constraint.kind.value for item in draft.constraint_decisions} == {
            "walking_intensity",
            "interest",
            "meal",
        }
        assert draft.proposal_can_confirm is True

    asyncio.run(exercise())


def test_confirmation_applies_proposal_and_promotes_constraints() -> None:
    async def exercise() -> None:
        service = RequestIntakeService(Settings(_env_file=None, environment="test"))
        draft = await service.propose(
            make_payload("带一个孩子去北京看科技馆, 不要寺庙, 预算5000, 轻松玩两天")
        )
        confirmed = await service.confirm(
            draft.draft_id,
            RequestIntakeConfirmRequest(
                selection="proposal",
                selected_destination_adcode="110000",
            ),
        )

        request = confirmed.request
        assert request.destination_city == "北京"
        assert request.destination_adcode == "110000"
        assert request.day_count == 2
        assert request.party.adults == 1
        assert request.party.children == 1
        assert request.budget is not None and request.budget.total_limit == 5000
        assert BudgetCategory.LODGING in request.budget.included_categories
        assert request.travel_styles == ("科技",)
        assert {item.kind.value for item in request.constraints.items} == {"avoid", "interest"}
        assert all(item.confirmed for item in request.constraints.items)
        assert all(item.source.value == "user_confirmed" for item in request.constraints.items)
        assert "轻步行" not in request.travel_styles

    asyncio.run(exercise())


def test_confirmation_can_keep_form_fields_and_preserve_text_preferences() -> None:
    async def exercise() -> None:
        service = RequestIntakeService(Settings(_env_file=None, environment="test"))
        draft = await service.propose(make_payload("去北京看科技馆, 不要寺庙"))
        confirmed = await service.confirm(
            draft.draft_id,
            RequestIntakeConfirmRequest(
                selection="form",
                selected_destination_adcode="110000",
            ),
        )

        request = confirmed.request
        assert request.destination_city == "北京"
        assert request.party.adults == 2
        assert request.travel_styles == ("科技",)
        assert {item.kind.value for item in request.constraints.items} == {
            "avoid",
            "interest",
        }
        assert all(item.confirmed for item in request.constraints.items)

    asyncio.run(exercise())


def test_normalizer_rejects_non_verbatim_evidence_and_duplicate_fields() -> None:
    missing_evidence = RequestIntakeModelResponse(
        proposal=RequestFieldProposalBatch(
            items=(
                RequestFieldProposalItem(
                    field="destination_city",
                    value="北京",
                    evidence="原文里没有北京",
                    evidence_mode="explicit",
                ),
            )
        ),
        model="test-model",
        latency_ms=1,
    )
    with pytest.raises(RequestIntakeProtocolError, match="exact raw-text span"):
        normalize_request_field_response(
            "request-intake-test",
            "去上海玩两天",
            date(2026, 8, 24),
            missing_evidence,
        )

    duplicate = RequestIntakeModelResponse(
        proposal=RequestFieldProposalBatch(
            items=(
                RequestFieldProposalItem(
                    field="adults",
                    value="2",
                    evidence="两位成年人",
                    evidence_mode="explicit",
                ),
                RequestFieldProposalItem(
                    field="adults",
                    value="3",
                    evidence="三位成年人",
                    evidence_mode="explicit",
                ),
            )
        ),
        model="test-model",
        latency_ms=1,
    )
    with pytest.raises(RequestIntakeProtocolError, match="duplicate request field"):
        normalize_request_field_response(
            "request-intake-test",
            "两位成年人或三位成年人",
            date(2026, 8, 24),
            duplicate,
        )


def test_normalizer_recomputes_date_and_budget_from_evidence() -> None:
    response = RequestIntakeModelResponse(
        proposal=RequestFieldProposalBatch(
            items=(
                RequestFieldProposalItem(
                    field="start_date",
                    value="2026-09-11",
                    evidence="九月十日",
                    evidence_mode="explicit",
                ),
                RequestFieldProposalItem(
                    field="budget_limit",
                    value="6000",
                    evidence="预算五千",
                    evidence_mode="explicit",
                ),
            )
        ),
        model="test-model",
        latency_ms=1,
    )

    result = normalize_request_field_response(
        "request-intake-test",
        "九月十日出发, 预算五千",
        date(2026, 8, 24),
        response,
    )

    assert result.proposed_fields.start_date is None
    assert result.proposed_fields.budget_limit is None
    assert {item.status.value for item in result.decisions} == {"needs_confirmation"}
    assert len(result.clarifications) == 2


class FakeCompletions:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: object) -> None:
        self.completions = FakeCompletions(response)
        self.chat = SimpleNamespace(completions=self.completions)


def test_deepseek_request_adapter_forces_schema_tool_without_control_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = json.dumps(
        {
            "items": [
                {
                    "field": "destination_city",
                    "value": "北京",
                    "evidence": "去北京玩",
                    "evidence_mode": "explicit",
                }
            ]
        },
        ensure_ascii=False,
    )
    tool_call = SimpleNamespace(
        type="function",
        function=SimpleNamespace(name=REQUEST_FIELD_TOOL_NAME, arguments=arguments),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))],
        usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10, total_tokens=40),
    )
    fake_client = FakeOpenAIClient(response)
    monkeypatch.setattr(agent_module, "OpenAI", lambda **_: fake_client)
    monkeypatch.setattr(agent_module, "wrap_openai", lambda client, **_: client)
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-test-secret"),
    )

    result = DeepSeekRequestFieldProposalModel(settings).propose(
        "去北京玩",
        date(2026, 8, 24),
    )

    assert result.usage is not None and result.usage.total_tokens == 40
    call = fake_client.completions.calls[0]
    assert call["tool_choice"]["function"]["name"] == REQUEST_FIELD_TOOL_NAME
    parameters = json.dumps(call["tools"][0]["function"]["parameters"])
    assert "confirmed" not in parameters
    assert "source" not in parameters
    assert "destination_adcode" not in parameters
    user_message = call["messages"][1]["content"]
    assert "用户原文: 去北京玩" in user_message
    assert RequestEvidenceMode.EXPLICIT.value == "explicit"
