from pathlib import Path
from tempfile import TemporaryDirectory

from app.agents.contracts import PlannerModelResponse
from app.agents.single_planner import PlannerProposalModel
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.sources import DataMode
from app.evaluation.checkpoint_contracts import (
    CheckpointHitlCase,
    CheckpointHitlCaseResult,
    CheckpointHitlReport,
    CheckpointHitlSuite,
    checkpoint_hitl_dataset_sha256,
)
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.vertical_slice import (
    FixturePlannerProposalModel,
    VerticalSliceScenarioProvider,
    load_vertical_slice_suite,
    vertical_slice_dataset_sha256,
)
from app.evaluation.vertical_slice_contracts import VerticalSliceCase
from app.planning import (
    HumanReviewResume,
    PlanningThreadStatus,
    open_sqlite_planning_runtime,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHECKPOINT_HITL_SUITE_PATH = (
    REPOSITORY_ROOT / "evals" / "cases" / "checkpoint-hitl" / "suite.v1.json"
)
CHECKPOINT_HITL_REPORT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "stateful-checkpoint-hitl.v1.json"
)


class CountingPlannerProposalModel(PlannerProposalModel):
    def __init__(self, delegate: PlannerProposalModel) -> None:
        self.delegate = delegate
        self.call_count = 0

    def propose(
        self,
        context: PlannerContext,
        candidates: tuple[CandidatePOI, ...],
    ) -> PlannerModelResponse:
        self.call_count += 1
        return self.delegate.propose(context, candidates)


def load_checkpoint_hitl_suite(
    path: Path = CHECKPOINT_HITL_SUITE_PATH,
) -> CheckpointHitlSuite:
    return CheckpointHitlSuite.model_validate_json(path.read_text(encoding="utf-8"))


def _fixture_dependencies(
    source_case: VerticalSliceCase,
) -> tuple[VerticalSliceScenarioProvider, CountingPlannerProposalModel]:
    provider = VerticalSliceScenarioProvider(source_case.provider_responses)
    model = CountingPlannerProposalModel(
        FixturePlannerProposalModel(
            PlannerModelResponse(
                proposal=source_case.planner_proposal,
                model=source_case.planner_model,
                latency_ms=0,
            )
        )
    )
    return provider, model


async def evaluate_checkpoint_hitl_case(
    case: CheckpointHitlCase,
    source_case: VerticalSliceCase,
) -> CheckpointHitlCaseResult:
    provider, model = _fixture_dependencies(source_case)
    restored_provider, restored_model = _fixture_dependencies(source_case)

    with TemporaryDirectory(prefix="eztrip-checkpoint-hitl-") as temp_dir:
        checkpoint_path = Path(temp_dir) / "checkpoint.sqlite"
        async with open_sqlite_planning_runtime(
            checkpoint_path,
            provider,
            model,
            clock=lambda: case.decided_at,
        ) as runtime:
            paused = await runtime.start(
                case.thread_id,
                source_case.request,
                source_case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
            review = paused.state.review_request
            if review is None or paused.state.vertical_slice is None:
                raise RuntimeError("checkpoint evaluation did not reach a complete review boundary")
            paused_vertical_slice = paused.state.vertical_slice
            checkpoint_written = checkpoint_path.exists() and checkpoint_path.stat().st_size > 0
        provider.verify_complete()

        async with open_sqlite_planning_runtime(
            checkpoint_path,
            restored_provider,
            restored_model,
            clock=lambda: case.decided_at,
        ) as restored_runtime:
            restored = await restored_runtime.snapshot(case.thread_id)
            state_restored = restored.state == paused.state
            terminal = await restored_runtime.resume(
                case.thread_id,
                HumanReviewResume(
                    review_id=review.review_id,
                    action=case.action,
                    reviewer_id=case.reviewer_id,
                    comment=case.comment,
                ),
            )
            history = await restored_runtime.history(case.thread_id)

    decision = terminal.state.review_decision
    event_nodes = tuple(event.node for event in terminal.state.events)
    vertical_slice_preserved = (
        terminal.state.vertical_slice == paused_vertical_slice
        and terminal.state.vertical_slice is not None
        and terminal.state.vertical_slice.plan.status.value == "draft"
    )
    checks = (
        EvaluationCheck(
            code="paused_at_native_human_review",
            passed=paused.state.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW
            and paused.next_nodes == ("human_review",),
        ),
        EvaluationCheck(
            code="deterministic_review_policy",
            passed=review.kind == case.expected_review_kind
            and case.action in review.allowed_actions,
        ),
        EvaluationCheck(code="sqlite_checkpoint_written", passed=checkpoint_written),
        EvaluationCheck(
            code="state_restored_after_runtime_rebuild",
            passed=state_restored,
        ),
        EvaluationCheck(
            code="provider_not_replayed_after_restore",
            passed=len(restored_provider.calls) == 0,
        ),
        EvaluationCheck(
            code="planner_model_not_replayed_after_restore",
            passed=restored_model.call_count == 0,
        ),
        EvaluationCheck(
            code="human_decision_audited",
            passed=decision is not None
            and decision.review_id == review.review_id
            and decision.action == case.action
            and decision.reviewer_id == case.reviewer_id
            and decision.comment == case.comment
            and decision.decided_at == case.decided_at,
        ),
        EvaluationCheck(
            code="expected_terminal_status",
            passed=terminal.state.status == case.expected_terminal_status
            and terminal.next_nodes == (),
        ),
        EvaluationCheck(
            code="vertical_slice_preserved_as_draft",
            passed=vertical_slice_preserved,
        ),
        EvaluationCheck(
            code="checkpoint_history_covers_all_main_nodes",
            passed=len(history) >= 6
            and event_nodes
            == (
                "run_vertical_slice",
                "prepare_human_review",
                "human_review",
                "apply_review_decision",
            ),
        ),
    )
    return CheckpointHitlCaseResult(
        case_id=case.case_id,
        source_case_id=case.source_case_id,
        passed=all(check.passed for check in checks),
        paused_status=paused.state.status,
        review_kind=review.kind,
        action=case.action,
        terminal_status=terminal.state.status,
        provider_call_count=len(provider.calls),
        planner_model_call_count=model.call_count,
        restored_provider_call_count=len(restored_provider.calls),
        restored_planner_model_call_count=restored_model.call_count,
        checkpoint_count=len(history),
        state_restored_after_runtime_rebuild=state_restored,
        vertical_slice_preserved=vertical_slice_preserved,
        event_nodes=event_nodes,
        checks=checks,
    )


async def evaluate_checkpoint_hitl_suite(
    path: Path = CHECKPOINT_HITL_SUITE_PATH,
) -> CheckpointHitlReport:
    suite = load_checkpoint_hitl_suite(path)
    source_suite = load_vertical_slice_suite()
    source_hash = vertical_slice_dataset_sha256(source_suite)
    if suite.source_dataset_sha256 != source_hash:
        raise ValueError("checkpoint HITL suite source dataset hash has drifted")
    source_cases = {case.case_id: case for case in source_suite.cases}
    results = tuple(
        [
            await evaluate_checkpoint_hitl_case(case, source_cases[case.source_case_id])
            for case in suite.cases
        ]
    )
    checks = tuple(check for result in results for check in result.checks)
    passed_case_count = sum(result.passed for result in results)
    passed_check_count = sum(check.passed for check in checks)
    return CheckpointHitlReport(
        source_dataset_sha256=source_hash,
        dataset_sha256=checkpoint_hitl_dataset_sha256(suite),
        passed_case_count=passed_case_count,
        case_pass_rate=expected_rate(passed_case_count, len(results)),
        check_count=len(checks),
        passed_check_count=passed_check_count,
        check_pass_rate=expected_rate(passed_check_count, len(checks)),
        runtime_reconstruction_count=sum(
            result.state_restored_after_runtime_rebuild for result in results
        ),
        no_expensive_replay_count=sum(
            result.restored_provider_call_count == 0
            and result.restored_planner_model_call_count == 0
            for result in results
        ),
        draft_preserved_count=sum(result.vertical_slice_preserved for result in results),
        results=results,
        limitations=(
            "本报告使用版本化北京 fixture, 不代表实时旅行数据或模型规划质量。",
            "SQLite 只验证本地运行时重建, 尚未覆盖进程强杀、并发恢复或分布式部署。",
            "检查点包含结构化请求与规划状态; 生产环境仍需加密、保留期和访问控制。",
            "人审决定是固定测试输入, 尚未接入前端审核界面或身份认证。",
        ),
    )
