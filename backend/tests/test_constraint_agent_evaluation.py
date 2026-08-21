import json

from jsonschema import Draft202012Validator

from app.agents.constraint_agent import run_constraint_agent
from app.agents.contracts import (
    ConstraintAgentResult,
    ConstraintEvidenceMode,
    ConstraintModelResponse,
    ConstraintProposalBatch,
    ConstraintProposalItem,
)
from app.domain.request import ConstraintSource, TripRequest
from app.evaluation import (
    ConstraintAgentBaselineReport,
    ConstraintAgentExpectationSuite,
    ConstraintEvaluationLabel,
    evaluate_constraint_agent_suite,
    load_constraint_agent_expectations,
    load_planning_seed_suite,
)
from scripts.export_constraint_agent_schemas import (
    DEFAULT_EXPECTATION_SCHEMA_PATH,
    DEFAULT_REPORT_SCHEMA_PATH,
)
from scripts.run_constraint_agent_eval import DEFAULT_OUTPUT_PATH


class RequestBackedFixtureModel:
    def __init__(
        self,
        request: TripRequest,
        expected: tuple[ConstraintEvaluationLabel, ...],
    ) -> None:
        self.request = request
        self.expected = expected

    def propose(self, raw_text: str) -> ConstraintModelResponse:
        assert raw_text == self.request.raw_text
        proposals = tuple(
            _proposal_from_expected(self.request, constraint) for constraint in self.expected
        )
        return ConstraintModelResponse(
            proposal=ConstraintProposalBatch(items=proposals),
            model="request-backed-fixture",
            latency_ms=0,
        )


class InvalidEvidenceFixtureModel:
    def propose(self, raw_text: str) -> ConstraintModelResponse:
        return ConstraintModelResponse(
            proposal=ConstraintProposalBatch(
                items=(
                    ConstraintProposalItem(
                        kind="must_visit",
                        value="不存在的景点",
                        strength="hard",
                        priority=5,
                        evidence="不在输入中的证据",
                        evidence_mode="explicit",
                    ),
                )
            ),
            model="invalid-evidence-fixture",
            latency_ms=0,
        )


def _proposal_evidence(
    request: TripRequest,
    constraint: ConstraintEvaluationLabel,
) -> str:
    if constraint.source == ConstraintSource.AGENT_INFERRED:
        return request.raw_text
    if constraint.kind.value == "walking_intensity":
        return "轻步行" if "轻步行" in request.raw_text else "少走路"
    if not isinstance(constraint.value, str) or constraint.value not in request.raw_text:
        raise AssertionError("frozen Agent seed requires exact string evidence")
    return constraint.value


def _proposal_from_expected(
    request: TripRequest,
    constraint: ConstraintEvaluationLabel,
) -> ConstraintProposalItem:
    return ConstraintProposalItem(
        kind=constraint.kind,
        value=constraint.value,
        strength=constraint.strength,
        priority=5 if constraint.strength.value == "hard" else 3,
        evidence=_proposal_evidence(request, constraint),
        evidence_mode=(
            ConstraintEvidenceMode.INFERRED
            if constraint.source == ConstraintSource.AGENT_INFERRED
            else ConstraintEvidenceMode.EXPLICIT
        ),
    )


_, SEED_CASES = load_planning_seed_suite()
EXPECTATIONS = load_constraint_agent_expectations()
EXPECTATION_BY_CASE_ID = {item.case_id: item for item in EXPECTATIONS.cases}
EXPECTED_BY_REQUEST_ID = {
    case.request.request_id: EXPECTATION_BY_CASE_ID[case.case_id].expected_constraints
    for case in SEED_CASES
}


def fixture_runner(request: TripRequest) -> ConstraintAgentResult:
    return run_constraint_agent(
        request,
        RequestBackedFixtureModel(request, EXPECTED_BY_REQUEST_ID[request.request_id]),
    )


def invalid_runner(request: TripRequest) -> ConstraintAgentResult:
    return run_constraint_agent(request, InvalidEvidenceFixtureModel())


def test_same_ten_case_suite_measures_constraint_and_confirmation_contracts() -> None:
    report = evaluate_constraint_agent_suite(
        fixture_runner,
        execution_mode="fixture",
        model="request-backed-fixture",
    )

    assert report.case_count == 10
    assert report.passed_case_count == 10
    assert report.expected_constraint_count == 16
    assert report.actual_constraint_count == 16
    assert report.semantic_match_count == 16
    assert str(report.semantic_precision) == "1.0000"
    assert str(report.semantic_recall) == "1.0000"
    assert str(report.confirmation_accuracy) == "1.0000"
    assert report.clarification_match_case_count == 10
    assert report.usage_case_count == 0
    assert report.total_tokens == 0


def test_protocol_failure_is_recorded_without_storing_exception_text() -> None:
    report = evaluate_constraint_agent_suite(
        invalid_runner,
        execution_mode="fixture",
        model="invalid-evidence-fixture",
    )

    assert report.passed_case_count == 0
    assert report.semantic_match_count == 0
    assert {item.error_code for item in report.results} == {"agent-protocol-error"}
    serialized = report.model_dump_json()
    assert "不在输入中的证据" not in serialized
    assert "constraint evidence is not an exact raw-text span" not in serialized


def test_committed_expectations_and_live_report_match_exported_schemas() -> None:
    expectation_payload = json.loads(DEFAULT_EXPECTATION_SCHEMA_PATH.read_text(encoding="utf-8"))
    report_payload = json.loads(DEFAULT_REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    committed_expectations = json.loads(
        (
            DEFAULT_EXPECTATION_SCHEMA_PATH.parent.parent
            / "cases"
            / "constraint-agent"
            / "expectations.v1.json"
        ).read_text(encoding="utf-8")
    )
    committed_report = json.loads(DEFAULT_OUTPUT_PATH.read_text(encoding="utf-8"))

    assert expectation_payload == ConstraintAgentExpectationSuite.model_json_schema(
        mode="validation"
    )
    assert report_payload == ConstraintAgentBaselineReport.model_json_schema(mode="validation")
    Draft202012Validator(expectation_payload).validate(committed_expectations)
    Draft202012Validator(report_payload).validate(committed_report)

    report = ConstraintAgentBaselineReport.model_validate(committed_report)
    assert report.execution_mode == "live"
    assert report.model == "deepseek-v4-pro"
    assert report.passed_case_count == 9
    assert report.semantic_match_count == 15
    assert report.expected_constraint_count == 16
    assert report.confirmation_match_count == 15
    assert report.clarification_match_case_count == 10
    assert report.usage_case_count == 10
    assert report.total_tokens == 10362
