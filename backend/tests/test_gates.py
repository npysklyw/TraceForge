from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from benchwarden.application.comparison import CaseComparison, RunComparison
from benchwarden.application.gates import GatePolicy, evaluate_policy
from benchwarden.application.scoring import ScoringConflict
from benchwarden.scoring.metrics import aggregate


def metrics(**changes):
    values = dict(
        total_cases=10,
        execution_errors=2,
        pass_rate=0.9,
        end_to_end_success_rate=0.8,
        evaluation_coverage=0.8,
        latency_cases=10,
        p95_latency_ms=120,
        total_tokens=120,
        estimated_cost_usd=Decimal("1.20"),
    )
    values.update(changes)
    return aggregate([]).model_copy(update=values)


def comparison(candidate, **baseline_changes):
    return RunComparison(
        baseline_run_id=uuid4(),
        candidate_run_id=uuid4(),
        baseline=metrics(
            p95_latency_ms=100,
            total_tokens=100,
            estimated_cost_usd=Decimal("1"),
            **baseline_changes,
        ),
        candidate=candidate,
        deltas={},
        failure_category_deltas={},
        pricing_changed=False,
        identical_case_set=True,
        cases=[
            CaseComparison(
                test_case_id=uuid4(),
                classification="regressed",
                baseline_result_id=uuid4(),
                candidate_result_id=uuid4(),
            )
        ],
    )


@pytest.mark.parametrize(
    ("field", "boundary", "fails"),
    [
        ("min_pass_rate", "0.9", "0.90001"),
        ("min_end_to_end_success_rate", "0.8", "0.80001"),
        ("min_evaluation_coverage", "0.8", "0.80001"),
        ("max_execution_errors", 2, 1),
        ("max_execution_error_rate", "0.2", "0.19999"),
        ("max_latency_regression", "0.2", "0.19999"),
        ("max_latency_increase_ms", "20", "19.999"),
        ("max_token_increase", "0.2", "0.19999"),
        ("max_cost_increase", "0.2", "0.19999"),
        ("max_regressions", 1, 0),
    ],
)
def test_inclusive_threshold_boundaries(field, boundary, fails):
    candidate = metrics()
    for threshold, expected in [(boundary, True), (fails, False)]:
        checks = evaluate_policy(candidate, GatePolicy(**{field: threshold}), comparison(candidate))
        assert len(checks) == 1
        assert checks[0].passed is expected


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"min_pass_rate": "90"},
        {"min_pass_rate": "-0.1"},
        {"max_cost_increase": "NaN"},
        {"max_token_increase": "Infinity"},
        {"max_latency_regression": "-1"},
        {"max_regressions": 1.5},
        {"max_execution_errors": True},
        {"unknown": 0},
    ],
)
def test_invalid_policy(policy):
    with pytest.raises(ValidationError):
        GatePolicy(**policy)


@pytest.mark.parametrize(
    ("metric", "policy"),
    [
        ("pass_rate", {"min_pass_rate": 0}),
        ("evaluation_coverage", {"min_evaluation_coverage": 0}),
        ("end_to_end_success_rate", {"min_end_to_end_success_rate": 0}),
        ("total_tokens", {"max_token_increase": 100}),
        ("estimated_cost_usd", {"max_cost_increase": 100}),
        ("p95_latency_ms", {"max_latency_regression": 100}),
    ],
)
def test_unavailable_metrics_never_become_zero(metric, policy):
    candidate = metrics(**{metric: None})
    check = evaluate_policy(candidate, GatePolicy(**policy), comparison(candidate))[0]
    assert not check.passed
    assert check.observed is None
    assert check.reason == "metric_unavailable"


@pytest.mark.parametrize("after", [0, 1])
def test_zero_baseline(after):
    candidate = metrics(total_tokens=after)
    pair = comparison(candidate)
    pair.baseline.total_tokens = 0
    check = evaluate_policy(candidate, GatePolicy(max_token_increase=Decimal("100")), pair)[0]
    assert check.passed is (after == 0)
    assert check.reason == ("satisfied" if after == 0 else "zero_baseline")


def test_partial_latency_and_empty_error_rate():
    candidate = metrics(latency_cases=9)
    check = evaluate_policy(
        candidate, GatePolicy(max_latency_increase_ms=100), comparison(candidate)
    )[0]
    assert check.reason == "metric_unavailable"
    check = evaluate_policy(aggregate([]), GatePolicy(max_execution_error_rate=0))[0]
    assert check.reason == "metric_unavailable"


def test_relative_requires_baseline_and_improvements_pass():
    with pytest.raises(ScoringConflict):
        evaluate_policy(metrics(), GatePolicy(max_regressions=0))
    candidate = metrics(total_tokens=50)
    check = evaluate_policy(candidate, GatePolicy(max_token_increase=0), comparison(candidate))[0]
    assert check.passed
    assert check.observed == Decimal("-0.5")
