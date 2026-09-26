"""Read-only, fail-closed regression policies over the existing metric contracts."""

from decimal import Decimal, localcontext
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from benchwarden.application.comparison import RunComparison, compare_runs, validate_scores
from benchwarden.application.scoring import ScoringConflict, results_for_run
from benchwarden.persistence.models import EvaluationRun
from benchwarden.scoring.metrics import RunMetrics, aggregate

Rate = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Increase = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, le=1000000)]
Count = Annotated[int, Field(ge=0, strict=True)]


class GatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_pass_rate: Rate | None = None
    min_end_to_end_success_rate: Rate | None = None
    min_evaluation_coverage: Rate | None = None
    max_execution_errors: Count | None = None
    max_execution_error_rate: Rate | None = None
    max_latency_regression: Increase | None = None
    max_latency_increase_ms: Increase | None = None
    max_token_increase: Increase | None = None
    max_cost_increase: Increase | None = None
    max_regressions: Count | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("Configure at least one threshold")
        return self

    @property
    def needs_baseline(self) -> bool:
        return any(
            value is not None
            for value in (
                self.max_latency_regression,
                self.max_latency_increase_ms,
                self.max_token_increase,
                self.max_cost_increase,
                self.max_regressions,
            )
        )


class GateCheck(BaseModel):
    name: str
    passed: bool
    observed: Decimal | None
    threshold: Decimal
    unit: Literal["rate", "count", "milliseconds", "fractional_increase"]
    reason: Literal["satisfied", "threshold_exceeded", "metric_unavailable", "zero_baseline"]


class GateReport(BaseModel):
    run_id: UUID
    baseline_run_id: UUID | None
    passed: bool
    metrics: RunMetrics
    checks: list[GateCheck]


def number(value: int | float | Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def evaluate_policy(
    metrics: RunMetrics, policy: GatePolicy, comparison: RunComparison | None = None
) -> list[GateCheck]:
    if policy.needs_baseline and comparison is None:
        raise ScoringConflict("Relative thresholds and regression counts require --baseline")
    checks: list[GateCheck] = []

    def absolute(
        name: str,
        value: int | float | Decimal | None,
        threshold: Decimal | int | None,
        unit: Literal["rate", "count", "milliseconds"],
        *,
        minimum: bool = False,
    ) -> None:
        if threshold is None:
            return
        observed, limit = number(value), Decimal(threshold)
        passed = observed is not None and (observed >= limit if minimum else observed <= limit)
        checks.append(
            GateCheck(
                name=name,
                passed=passed,
                observed=observed,
                threshold=limit,
                unit=unit,
                reason="metric_unavailable"
                if observed is None
                else ("satisfied" if passed else "threshold_exceeded"),
            )
        )

    absolute("min_pass_rate", metrics.pass_rate, policy.min_pass_rate, "rate", minimum=True)
    absolute(
        "min_end_to_end_success_rate",
        metrics.end_to_end_success_rate,
        policy.min_end_to_end_success_rate,
        "rate",
        minimum=True,
    )
    absolute(
        "min_evaluation_coverage",
        metrics.evaluation_coverage,
        policy.min_evaluation_coverage,
        "rate",
        minimum=True,
    )
    absolute("max_execution_errors", metrics.execution_errors, policy.max_execution_errors, "count")
    error_rate = (
        Decimal(metrics.execution_errors) / metrics.total_cases if metrics.total_cases else None
    )
    absolute("max_execution_error_rate", error_rate, policy.max_execution_error_rate, "rate")

    def relative(
        name: str,
        before: int | float | Decimal | None,
        after: int | float | Decimal | None,
        threshold: Decimal | None,
    ) -> None:
        if threshold is None:
            return
        a, b = number(before), number(after)
        observed = None
        reason: Literal["satisfied", "threshold_exceeded", "metric_unavailable", "zero_baseline"]
        if a is None or b is None:
            passed, reason = False, "metric_unavailable"
        elif a == 0:
            passed = b == 0
            observed = Decimal(0) if passed else None
            reason = "satisfied" if passed else "zero_baseline"
        else:
            with localcontext() as context:
                context.prec = 50
                observed = (b - a) / a
                passed = b <= a * (1 + threshold)
            reason = "satisfied" if passed else "threshold_exceeded"
        checks.append(
            GateCheck(
                name=name,
                passed=passed,
                observed=observed,
                threshold=threshold,
                unit="fractional_increase",
                reason=reason,
            )
        )

    if comparison is not None:
        baseline = comparison.baseline
        # Latency aggregates can be partial during execution; gates require full coverage.
        a = baseline.p95_latency_ms if baseline.latency_cases == baseline.total_cases else None
        b = metrics.p95_latency_ms if metrics.latency_cases == metrics.total_cases else None
        relative("max_latency_regression", a, b, policy.max_latency_regression)
        latency_delta = (
            Decimal(str(b)) - Decimal(str(a)) if a is not None and b is not None else None
        )
        absolute(
            "max_latency_increase_ms", latency_delta, policy.max_latency_increase_ms, "milliseconds"
        )
        relative(
            "max_token_increase",
            baseline.total_tokens,
            metrics.total_tokens,
            policy.max_token_increase,
        )
        relative(
            "max_cost_increase",
            baseline.estimated_cost_usd,
            metrics.estimated_cost_usd,
            policy.max_cost_increase,
        )
        absolute(
            "max_regressions",
            sum(c.classification == "regressed" for c in comparison.cases),
            policy.max_regressions,
            "count",
        )
    return checks


def check_run(
    session: Session, run: EvaluationRun, policy: GatePolicy, baseline: EvaluationRun | None = None
) -> GateReport:
    results = results_for_run(session, run.id)
    validate_scores(run, results)
    if not results:
        raise ScoringConflict("Cannot gate an empty run")
    metrics = aggregate(results)
    comparison = compare_runs(session, baseline, run) if baseline else None
    if comparison and not comparison.identical_case_set:
        raise ScoringConflict("Regression gates require identical case sets")
    if comparison and comparison.pricing_changed and policy.max_cost_increase is not None:
        raise ScoringConflict("Cost regression gates require identical pricing snapshots")
    checks = evaluate_policy(metrics, policy, comparison)
    return GateReport(
        run_id=run.id,
        baseline_run_id=baseline.id if baseline else None,
        passed=all(check.passed for check in checks),
        metrics=metrics,
        checks=checks,
    )
