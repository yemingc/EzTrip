from app.domain.validation import IssueSeverity, PlanValidationReport


def build_review_prompt(
    validation: PlanValidationReport,
    *,
    approval_prompt: str,
) -> str:
    if validation.can_finalize:
        return approval_prompt
    blocking_issues = tuple(
        issue for issue in validation.issues if issue.severity == IssueSeverity.ERROR
    )
    warnings = tuple(
        issue for issue in validation.issues if issue.severity == IssueSeverity.WARNING
    )
    details = "; ".join(issue.message for issue in blocking_issues[:2])
    warning_copy = f"另有 {len(warnings)} 项提醒。" if warnings else ""
    return (
        f"当前草案有 {len(blocking_issues)} 项关键问题尚未解决: {details}。"
        f"{warning_copy}请查看校验明细后选择保留为待验证草案、请求修改或取消。"
    )
