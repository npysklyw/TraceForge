"""Explicit denominators and coverage; unknown measurements remain null."""

from collections import Counter
from decimal import Decimal, localcontext
from math import ceil
from statistics import median
from typing import Literal

from pydantic import BaseModel

from traceforge.domain.status import CaseStatus
from traceforge.persistence.models import CaseResult

Outcome = Literal["passed", "failed", "not_scored"]


def outcome(result: CaseResult) -> Outcome:
    if (
        result.status != CaseStatus.COMPLETED
        or result.scored_at is None
        or not result.expectations_snapshot
    ):
        return "not_scored"
    expected = {(e["name"], e["type"]) for e in result.expectations_snapshot}
    if {(s.scorer_name, s.scorer_type) for s in result.scoring_results} != expected:
        return "not_scored"
    return "passed" if all(s.passed for s in result.scoring_results) else "failed"


class RunMetrics(BaseModel):
    total_cases: int
    completed_executions: int
    execution_errors: int
    scoreable_cases: int
    passed_cases: int
    failed_cases: int
    unscored_cases: int
    evaluation_coverage: float | None
    pass_rate: float | None
    end_to_end_success_rate: float | None
    output_correctness_rate: float | None
    tool_selection_accuracy: float | None
    tool_argument_accuracy: float | None
    median_latency_ms: float | None
    p95_latency_ms: float | None
    latency_cases: int
    total_tokens: int | None
    known_total_tokens: int
    token_usage_cases: int
    estimated_cost_usd: Decimal | None
    known_cost_usd: Decimal
    cost_cases: int
    failure_category_counts: dict[str, int]


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def aggregate(results: list[CaseResult]) -> RunMetrics:
    total = len(results)
    outcomes = Counter(outcome(r) for r in results)
    scoreable = sum(
        r.status == CaseStatus.COMPLETED and bool(r.expectations_snapshot) for r in results
    )
    failures: Counter[str] = Counter()
    category_passes: Counter[str] = Counter()
    category_cases: Counter[str] = Counter()
    for result in results:
        if result.status == CaseStatus.ERROR:
            failures["execution"] += 1
        if outcome(result) == "not_scored":
            continue
        categories: dict[str, list[bool]] = {}
        for item in result.scoring_results:
            category = str(item.details["category"])
            categories.setdefault(category, []).append(item.passed)
        for category, passes in categories.items():
            category_cases[category] += 1
            category_passes[category] += all(passes)
            if not all(passes):
                failures[category] += 1
    latencies = sorted(r.latency_ms for r in results if r.latency_ms is not None)
    # Error usage may be partial: expose the known sum without claiming complete coverage.
    tokens = [
        r.total_tokens
        for r in results
        if r.total_tokens is not None and r.status == CaseStatus.COMPLETED
    ]
    known_tokens = sum(r.total_tokens or 0 for r in results)
    costs = [r.estimated_cost_usd for r in results if r.estimated_cost_usd is not None]
    with localcontext() as context:
        context.prec = 50
        known_cost = sum(costs, Decimal(0))
    return RunMetrics(
        total_cases=total,
        completed_executions=sum(r.status == CaseStatus.COMPLETED for r in results),
        execution_errors=sum(r.status == CaseStatus.ERROR for r in results),
        scoreable_cases=scoreable,
        passed_cases=outcomes["passed"],
        failed_cases=outcomes["failed"],
        unscored_cases=outcomes["not_scored"],
        evaluation_coverage=ratio(outcomes["passed"] + outcomes["failed"], total),
        pass_rate=ratio(outcomes["passed"], scoreable),
        end_to_end_success_rate=ratio(outcomes["passed"], total),
        output_correctness_rate=ratio(
            category_passes["output_correctness"], category_cases["output_correctness"]
        ),
        tool_selection_accuracy=ratio(
            category_passes["tool_selection"], category_cases["tool_selection"]
        ),
        tool_argument_accuracy=ratio(
            category_passes["tool_arguments"], category_cases["tool_arguments"]
        ),
        median_latency_ms=median(latencies) if latencies else None,
        p95_latency_ms=latencies[ceil(0.95 * len(latencies)) - 1] if latencies else None,
        latency_cases=len(latencies),
        total_tokens=sum(tokens) if total and len(tokens) == total else None,
        known_total_tokens=known_tokens,
        token_usage_cases=len(tokens),
        estimated_cost_usd=known_cost if total and len(costs) == total else None,
        known_cost_usd=known_cost,
        cost_cases=len(costs),
        failure_category_counts=dict(sorted(failures.items())),
    )
