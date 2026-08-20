import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SMOKE_CASE_DIRECTORY = REPOSITORY_ROOT / "evals" / "cases" / "smoke"
MANIFEST_PATH = SMOKE_CASE_DIRECTORY / "manifest.json"
SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "smoke-case.schema.json"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def load_manifest_cases() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    manifest = load_json(MANIFEST_PATH)
    return [(entry, load_json(SMOKE_CASE_DIRECTORY / entry["path"])) for entry in manifest["cases"]]


def expectation_codes(case: dict[str, Any]) -> set[str]:
    return {expectation["code"] for expectation in case["expectations"] if expectation["required"]}


def forbidden_codes(case: dict[str, Any]) -> set[str]:
    return {behavior["code"] for behavior in case["forbidden_behaviors"]}


def test_manifest_defines_one_case_per_smoke_category() -> None:
    manifest = load_json(MANIFEST_PATH)
    entries = manifest["cases"]

    assert manifest["suite"] == "gate-0-smoke"
    assert manifest["version"] == 1
    assert manifest["status"] == "specification"
    assert manifest["case_schema"] == "../../schemas/smoke-case.schema.json"
    assert len(entries) == 3
    assert {entry["category"] for entry in entries} == {
        "normal",
        "budget_conflict",
        "weather_risk",
    }
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert len({entry["path"] for entry in entries}) == len(entries)


def test_every_manifest_case_matches_the_json_schema() -> None:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    for entry, case in load_manifest_cases():
        errors = sorted(
            validator.iter_errors(case),
            key=lambda error: "/".join(str(part) for part in error.absolute_path),
        )
        assert not errors, "\n".join(error.message for error in errors)
        assert case["id"] == entry["id"]
        assert case["category"] == entry["category"]
        assert case["id"].endswith(f"-v{case['version']}")


def test_each_category_has_its_required_behavior_contract() -> None:
    required_codes = {
        "normal": {
            "request.constraints_preserved",
            "plan.day_count_is_three",
            "budget.recalculable",
            "recommendation.source_traceable",
        },
        "budget_conflict": {
            "validator.unsatisfiable_conflict",
            "constraint.no_silent_relaxation",
            "hitl.relaxation_requested",
            "plan.not_finalized",
        },
        "weather_risk": {
            "weather.tool_originated_detection",
            "weather.risk_structured",
            "plan.impacted_items_identified",
            "repair.local_replan_proposed",
            "hitl.major_change_requires_confirmation",
        },
    }

    for _, case in load_manifest_cases():
        assert required_codes[case["category"]] <= expectation_codes(case)


def test_budget_conflict_is_deterministically_unsatisfiable() -> None:
    cases = {case["category"]: case for _, case in load_manifest_cases()}
    budget_case = cases["budget_conflict"]
    cost_floor = budget_case["given"]["conditions"][0]["values"]

    assert cost_floor["deterministic_floor"] > cost_floor["requested_total"]
    assert "budget.exceeded_without_warning" in forbidden_codes(budget_case)
    assert "constraint.silently_removed" in forbidden_codes(budget_case)


def test_weather_risk_originates_from_the_tool_not_the_user() -> None:
    cases = {case["category"]: case for _, case in load_manifest_cases()}
    weather_case = cases["weather_risk"]
    request_text = weather_case["request"]["text"]
    weather_condition = weather_case["given"]["conditions"][0]

    assert all(term not in request_text for term in ("下雨", "降雨", "天气", "预报"))
    assert weather_condition["kind"] == "weather_observation"
    assert weather_condition["source"] == "weather_tool_fixture"
    assert weather_condition["values"]["day_offset"] == 2
    assert "weather.tool_originated_detection" in expectation_codes(weather_case)
    assert "weather.waits_for_user_report" in forbidden_codes(weather_case)
