import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Literal

from openai import OpenAIError
from pydantic import ValidationError

from app.agents.constraint_agent import (
    ConstraintAgentConfigurationError,
    ConstraintAgentProtocolError,
    canonicalize_constraint_value,
)
from app.agents.contracts import ConstraintAgentResult
from app.domain.request import Constraint, TripRequest
from app.evaluation.contracts import (
    ConstraintAgentBaselineReport,
    ConstraintAgentCaseResult,
    ConstraintAgentExpectationCase,
    ConstraintAgentExpectationSuite,
    ConstraintEvaluationLabel,
    EvaluationCheck,
    PlanningSeedCase,
    expected_rate,
)
from app.evaluation.planning_seed import (
    PLANNING_SEED_MANIFEST_PATH,
    load_planning_seed_suite,
    planning_seed_dataset_sha256,
)

ConstraintAgentRunner = Callable[[TripRequest], ConstraintAgentResult]
SemanticSignature = tuple[str, str, str]
ConfirmationSignature = tuple[str, str, str, str, bool]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONSTRAINT_AGENT_EXPECTATIONS_PATH = (
    REPOSITORY_ROOT / "evals" / "cases" / "constraint-agent" / "expectations.v1.json"
)


class ConstraintAgentEvaluationError(RuntimeError):
    """Raised when the frozen seed suite cannot be compared to Agent output."""


def _label_from_constraint(constraint: Constraint) -> ConstraintEvaluationLabel:
    if not isinstance(constraint.value, str):
        raise ConstraintAgentEvaluationError(
            "constraint Agent V1 evaluation supports string constraint values only"
        )
    return ConstraintEvaluationLabel(
        kind=constraint.kind,
        value=canonicalize_constraint_value(constraint.kind, constraint.value),
        strength=constraint.strength,
        source=constraint.source,
        confirmed=constraint.confirmed,
    )


def _semantic_signature(label: ConstraintEvaluationLabel) -> SemanticSignature:
    value = canonicalize_constraint_value(label.kind, label.value)
    return (label.kind.value, value.casefold(), label.strength.value)


def _confirmation_signature(label: ConstraintEvaluationLabel) -> ConfirmationSignature:
    semantic = _semantic_signature(label)
    return (*semantic, label.source.value, label.confirmed)


def load_constraint_agent_expectations(
    expectation_path: Path = CONSTRAINT_AGENT_EXPECTATIONS_PATH,
) -> ConstraintAgentExpectationSuite:
    return ConstraintAgentExpectationSuite.model_validate_json(
        expectation_path.read_text(encoding="utf-8")
    )


def constraint_agent_dataset_sha256(
    cases: tuple[PlanningSeedCase, ...],
    expectations: ConstraintAgentExpectationSuite,
) -> str:
    expected_by_id = {item.case_id: item for item in expectations.cases}
    canonical = json.dumps(
        [
            {
                "case_id": seed_case.case_id,
                "raw_text": seed_case.request.raw_text,
                "expected_constraints": [
                    item.model_dump(mode="json")
                    for item in expected_by_id[seed_case.case_id].expected_constraints
                ],
            }
            for seed_case in cases
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _error_code(error: Exception) -> str:
    if isinstance(error, ConstraintAgentConfigurationError):
        return "agent-configuration-error"
    if isinstance(error, ConstraintAgentProtocolError):
        return "agent-protocol-error"
    if isinstance(error, ValidationError):
        return "schema-validation-error"
    return "model-provider-error"


def evaluate_constraint_agent_case(
    seed_case: PlanningSeedCase,
    expectation: ConstraintAgentExpectationCase,
    runner: ConstraintAgentRunner,
) -> ConstraintAgentCaseResult:
    if expectation.case_id != seed_case.case_id:
        raise ConstraintAgentEvaluationError("expectation does not match planning seed case")
    expected_constraints = expectation.expected_constraints
    expected_semantic = {_semantic_signature(item) for item in expected_constraints}
    expected_confirmation = {_confirmation_signature(item) for item in expected_constraints}
    expected_pending = {
        _semantic_signature(item) for item in expected_constraints if not item.confirmed
    }

    started = perf_counter()
    result: ConstraintAgentResult | None = None
    error_code: str | None = None
    try:
        result = runner(seed_case.request)
    except (
        ConstraintAgentConfigurationError,
        ConstraintAgentProtocolError,
        OpenAIError,
        ValidationError,
    ) as error:
        error_code = _error_code(error)
    elapsed_ms = round((perf_counter() - started) * 1000)

    actual_constraints = (
        tuple(_label_from_constraint(item) for item in result.constraints.items)
        if result is not None
        else ()
    )
    actual_semantic = {_semantic_signature(item) for item in actual_constraints}
    actual_confirmation = {_confirmation_signature(item) for item in actual_constraints}
    actual_pending = {
        _semantic_signature(item) for item in actual_constraints if not item.confirmed
    }
    semantic_match_count = len(expected_semantic & actual_semantic)
    confirmation_match_count = len(expected_confirmation & actual_confirmation)
    clarification_match = actual_pending == expected_pending
    checks = (
        EvaluationCheck(code="protocol_success", passed=result is not None),
        EvaluationCheck(
            code="semantic_constraints_exact",
            passed=actual_semantic == expected_semantic,
        ),
        EvaluationCheck(
            code="source_confirmation_exact",
            passed=actual_confirmation == expected_confirmation,
        ),
        EvaluationCheck(
            code="clarification_set_exact",
            passed=clarification_match,
        ),
    )
    return ConstraintAgentCaseResult(
        case_id=seed_case.case_id,
        tier=seed_case.tier,
        passed=all(check.passed for check in checks),
        expected_constraints=expected_constraints,
        actual_constraints=actual_constraints,
        expected_constraint_count=len(expected_constraints),
        actual_constraint_count=len(actual_constraints),
        semantic_match_count=semantic_match_count,
        confirmation_match_count=confirmation_match_count,
        clarification_match=clarification_match,
        latency_ms=elapsed_ms,
        usage=result.usage if result is not None else None,
        error_code=error_code,
        checks=checks,
    )


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return sorted(values)[max(rank - 1, 0)]


def evaluate_constraint_agent_suite(
    runner: ConstraintAgentRunner,
    *,
    execution_mode: Literal["fixture", "live"],
    model: str,
) -> ConstraintAgentBaselineReport:
    _, cases = load_planning_seed_suite(PLANNING_SEED_MANIFEST_PATH)
    expectations = load_constraint_agent_expectations()
    planning_seed_sha256 = planning_seed_dataset_sha256(cases)
    if expectations.source_planning_seed_sha256 != planning_seed_sha256:
        raise ConstraintAgentEvaluationError(
            "constraint expectations reference a different planning seed dataset"
        )
    expected_by_id = {item.case_id: item for item in expectations.cases}
    if set(expected_by_id) != {item.case_id for item in cases}:
        raise ConstraintAgentEvaluationError(
            "constraint expectation inventory must match planning seed cases"
        )
    results = tuple(
        evaluate_constraint_agent_case(case, expected_by_id[case.case_id], runner) for case in cases
    )
    passed_case_count = sum(item.passed for item in results)
    expected_count = sum(item.expected_constraint_count for item in results)
    actual_count = sum(item.actual_constraint_count for item in results)
    semantic_match_count = sum(item.semantic_match_count for item in results)
    confirmation_match_count = sum(item.confirmation_match_count for item in results)
    clarification_match_count = sum(item.clarification_match for item in results)
    usage = [item.usage for item in results if item.usage is not None]
    latencies = [item.latency_ms for item in results]
    limitations = [
        "评测只覆盖 10 条冻结中文请求, 不能外推为生产抽取质量。",
        "精确匹配基于当前 ConstraintKind、规范值、strength、source 和 confirmed 契约。",
        "本阶段只替换 TripRequest 的 constraints slice, 不解析日期、人数、预算或城市。",
        "Constraint Agent 不生成景点事实、价格、天气、路线或逐日行程。",
    ]
    if execution_mode == "fixture":
        limitations.append("fixture 模式验证评测器和 Agent 边界, 不代表 DeepSeek 模型质量。")
    else:
        limitations.append("live 结果是单次 point-in-time 样本, 会随模型和服务状态变化。")
    return ConstraintAgentBaselineReport(
        execution_mode=execution_mode,
        model=model,
        dataset_sha256=constraint_agent_dataset_sha256(cases, expectations),
        passed_case_count=passed_case_count,
        exact_case_rate=expected_rate(passed_case_count, len(results)),
        expected_constraint_count=expected_count,
        actual_constraint_count=actual_count,
        semantic_match_count=semantic_match_count,
        semantic_precision=expected_rate(semantic_match_count, actual_count),
        semantic_recall=expected_rate(semantic_match_count, expected_count),
        confirmation_match_count=confirmation_match_count,
        confirmation_accuracy=expected_rate(confirmation_match_count, semantic_match_count),
        clarification_match_case_count=clarification_match_count,
        clarification_case_rate=expected_rate(clarification_match_count, len(results)),
        usage_case_count=len(usage),
        total_prompt_tokens=sum(item.prompt_tokens for item in usage),
        total_completion_tokens=sum(item.completion_tokens for item in usage),
        total_tokens=sum(item.total_tokens for item in usage),
        p50_latency_ms=_nearest_rank(latencies, 50),
        p95_latency_ms=_nearest_rank(latencies, 95),
        results=results,
        limitations=tuple(limitations),
    )
