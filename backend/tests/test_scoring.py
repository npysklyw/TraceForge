from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from traceforge.application.comparison import compare_case, delta
from traceforge.domain.status import CaseStatus
from traceforge.persistence.models import CaseResult, ScoringResult
from traceforge.scoring.expectations import Pricing, parse_expectations
from traceforge.scoring.metrics import aggregate, outcome
from traceforge.scoring.scorers import Evidence, ToolEvidence, estimate_cost, json_equal, score


def evaluate(kind, evidence, **kwargs):
    expectation = parse_expectations([{"name": "test", "type": kind, **kwargs}])[0]
    return score(expectation, evidence)


@pytest.mark.parametrize(
    "expected,observed,passed",
    [
        (None, None, True),
        (True, 1, False),
        ({"x": [False]}, {"x": [0]}, False),
        (1, 1.0, True),
        ("1", 1, False),
        ({"a": None}, {}, False),
        ([1, 2], [2, 1], False),
        ("Hi", "hi", False),
    ],
)
def test_exact_json_semantics(expected, observed, passed):
    assert evaluate("exact_output", Evidence(output=observed), expected=expected).passed == passed


@pytest.mark.parametrize(
    "expected,observed,mode,passed",
    [
        ("CAFÉ", "cafe\u0301", "normalized", True),
        ("Straße", "STRASSE", "normalized", True),
        ("Ａ  B", "a\u00a0b", "normalized", True),
        ("hello!", "hello", "normalized", False),
        ("A B", " a  b ", "case_insensitive", False),
        ("Straße", "STRASSE", "case_insensitive", True),
        ("123", 123, "normalized", False),
        ("", " \n", "normalized", True),
    ],
)
def test_unicode_normalization(expected, observed, mode, passed):
    assert (
        evaluate(
            "normalized_output", Evidence(output=observed), expected=expected, mode=mode
        ).passed
        == passed
    )


def test_substring_boundaries_and_nontext():
    assert evaluate(
        "required_substring", Evidence(output="STRASSE"), expected="straße", case_sensitive=False
    ).passed
    assert not evaluate("required_substring", Evidence(output="hello"), expected="Hello").passed
    assert not evaluate(
        "required_substring", Evidence(output={"a": "hello"}), expected="hello"
    ).passed
    with pytest.raises(ValidationError):
        evaluate("required_substring", Evidence(), expected="")


def test_multiple_tool_selection_order_and_multiplicity():
    evidence = Evidence(tools=[ToolEvidence("a", {}), ToolEvidence("b", {}), ToolEvidence("a", {})])
    assert evaluate("tool_selection", evidence, expected=["a", "b", "a"]).passed
    assert not evaluate("tool_selection", evidence, expected=["a", "a", "b"]).passed
    assert evaluate("tool_selection", evidence, expected=["a", "a"], mode="required").passed
    assert not evaluate("tool_selection", evidence, expected=["a"] * 3, mode="required").passed
    assert evaluate("tool_selection", Evidence(), expected=[]).passed


def test_nested_arguments_exact_partial_occurrences_and_missing():
    args = {"order": {"id": 1, "other": 2}, "items": [{"a": 1, "b": 2}]}
    evidence = Evidence(tools=[ToolEvidence("get", {}), ToolEvidence("get", args)])
    assert evaluate(
        "tool_arguments",
        evidence,
        tool_name="get",
        occurrence=1,
        expected={"order": {"id": 1}},
        mode="partial",
    ).passed
    assert not evaluate(
        "tool_arguments", evidence, tool_name="get", occurrence=1, expected={"order": {"id": 1}}
    ).passed
    assert evaluate("tool_arguments", evidence, tool_name="get", occurrence=1, expected=args).passed
    assert not evaluate(
        "tool_arguments", evidence, tool_name="get", occurrence=2, expected={}
    ).passed
    assert not evaluate(
        "tool_arguments",
        Evidence(tools=[ToolEvidence("get", {}, False)]),
        tool_name="get",
        expected={},
        mode="partial",
    ).passed
    assert not json_equal({"items": [{"a": 1}]}, args, partial=True)  # Arrays are exact.
    assert not json_equal({"order": {"id": True}}, args, partial=True)
    assert not json_equal({"absent": None}, args, partial=True)


def test_inclusive_thresholds_and_missing_measurements():
    assert evaluate("max_latency", Evidence(latency_ms=10), maximum_ms=10).passed
    assert not evaluate("max_latency", Evidence(latency_ms=10.01), maximum_ms=10).passed
    assert not evaluate("max_latency", Evidence(), maximum_ms=10).passed
    assert evaluate(
        "max_cost", Evidence(cost_usd=Decimal("0.000001")), maximum_usd="0.000001"
    ).passed
    assert not evaluate(
        "max_cost", Evidence(cost_usd=Decimal("0.000001000001")), maximum_usd="0.000001"
    ).passed
    assert "unavailable" in evaluate("max_cost", Evidence(), maximum_usd=0).explanation


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "max_latency", "maximum_ms": -1},
        {"type": "max_latency", "maximum_ms": float("inf")},
        {"type": "max_cost", "maximum_usd": "NaN"},
        {"type": "max_cost", "maximum_usd": "-1"},
        {"type": "tool_arguments", "expected": {}, "tool_name": "get", "occurrence": True},
        {"type": "unknown"},
        {"type": "exact_output", "expected": 1, "unexpected": 1},
    ],
)
def test_invalid_expectations(payload):
    with pytest.raises(ValidationError):
        parse_expectations([{"name": "test", **payload}])


def test_unique_names_and_execution_errors():
    item = {"name": "a", "type": "exact_output", "expected": None}
    with pytest.raises(ValidationError):
        parse_expectations([item, item])
    with pytest.raises(ValueError, match="not scoreable"):
        score(parse_expectations([item])[0], Evidence(execution_succeeded=False))


def test_decimal_cost_is_exact_and_optional():
    pricing = Pricing(
        input_usd_per_million=Decimal("0.123456789012"), output_usd_per_million=Decimal("0.2")
    )
    assert estimate_cost(pricing, 1, 1) == Decimal("0.000000323456789012")
    assert estimate_cost(pricing, 0, 0) == 0
    assert estimate_cost(None, 1, 1) is None
    assert estimate_cost(pricing, None, 1) is None
    with pytest.raises(ValidationError):
        Pricing(currency="EUR", input_usd_per_million=1, output_usd_per_million=1)


def result(passed=True, status=CaseStatus.COMPLETED, expectations=True, latency=10):
    item = CaseResult(
        id=uuid4(),
        test_case_id=uuid4(),
        status=status,
        expectations_snapshot=[{"name": "a", "type": "exact_output"}] if expectations else [],
        scored_at=datetime.now(UTC),
        latency_ms=latency,
        total_tokens=10,
        estimated_cost_usd=Decimal("0.001"),
    )
    item.scoring_results = (
        [
            ScoringResult(
                scorer_name="a",
                scorer_type="exact_output",
                scorer_version="1",
                passed=passed,
                details={"category": "output_correctness"},
            )
        ]
        if expectations and status == CaseStatus.COMPLETED
        else []
    )
    return item


def test_aggregation_denominators_errors_and_unscored():
    items = [result(), result(False), result(status=CaseStatus.ERROR), result(expectations=False)]
    items[2].estimated_cost_usd = None
    metrics = aggregate(items)
    assert (metrics.total_cases, metrics.completed_executions, metrics.execution_errors) == (
        4,
        3,
        1,
    )
    assert (
        metrics.scoreable_cases,
        metrics.passed_cases,
        metrics.failed_cases,
        metrics.unscored_cases,
    ) == (2, 1, 1, 2)
    assert metrics.evaluation_coverage == 0.5
    assert metrics.pass_rate == metrics.output_correctness_rate == 0.5
    assert metrics.end_to_end_success_rate == 0.25
    assert metrics.tool_selection_accuracy is None
    assert metrics.failure_category_counts == {"execution": 1, "output_correctness": 1}
    assert metrics.total_tokens is None and metrics.known_total_tokens == 40
    assert metrics.estimated_cost_usd is None and metrics.known_cost_usd == Decimal(".003")
    assert outcome(items[2]) == outcome(items[3]) == "not_scored"


def test_empty_and_percentiles_and_pending_scores():
    empty = aggregate([])
    assert empty.total_cases == 0
    assert empty.pass_rate is empty.evaluation_coverage is empty.p95_latency_ms is None
    metrics = aggregate([result(latency=n) for n in range(1, 21)])
    assert metrics.median_latency_ms == 10.5 and metrics.p95_latency_ms == 19
    assert metrics.total_tokens == 200 and metrics.estimated_cost_usd == Decimal(".020")
    pending = result()
    pending.scored_at = None
    assert aggregate([pending]).evaluation_coverage == 0
    assert aggregate([pending]).pass_rate == 0


def test_comparison_classifications_and_deltas():
    a, b = result(False), result(True)
    assert compare_case(a, b).classification == "improved"
    assert compare_case(b, a).classification == "regressed"
    assert compare_case(a, a).classification == "unchanged"
    error = result(status=CaseStatus.ERROR)
    assert compare_case(error, b).classification == "improved"
    assert compare_case(a, error).classification == "regressed"
    assert delta(0, 1).absolute == 1 and delta(0, 1).percent is None
    assert delta(None, 1).absolute is None
    assert delta(2, 3).percent == 50


def test_mixed_assertion_changes_regression_takes_precedence():
    before, after = result(), result(False)
    for case, passed in [(before, False), (after, True)]:
        case.scoring_results.append(ScoringResult(scorer_name="other", passed=passed))
    comparison = compare_case(before, after)
    assert comparison.classification == "regressed"
    assert comparison.improved_expectations == ["other"]
    assert comparison.regressed_expectations == ["a"]


def test_multiple_assertions_count_once_per_category_and_exact_cost_sum():
    item = result()
    item.expectations_snapshot.append({"name": "b", "type": "required_substring"})
    item.scoring_results.append(
        ScoringResult(
            scorer_name="b",
            scorer_type="required_substring",
            passed=False,
            details={"category": "output_correctness"},
        )
    )
    item.estimated_cost_usd = Decimal("123456789012345.123456789012345678")
    metrics = aggregate([item])
    assert metrics.output_correctness_rate == 0
    assert metrics.failure_category_counts == {"output_correctness": 1}
    assert metrics.estimated_cost_usd == item.estimated_cost_usd
