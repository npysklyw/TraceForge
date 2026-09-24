"""Compare compatible finished experiments without modifying either run."""

from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.orm import Session

from traceforge.application.scoring import ScoringConflict, require_finished, results_for_run
from traceforge.domain.status import CaseStatus
from traceforge.persistence.models import CaseResult, EvaluationRun
from traceforge.scoring.metrics import RunMetrics, aggregate, outcome
from traceforge.scoring.scorers import json_equal


class MetricDelta(BaseModel):
    absolute: Decimal | None
    percent: Decimal | None


class CaseComparison(BaseModel):
    test_case_id: UUID
    classification: Literal["improved", "regressed", "unchanged", "unmatched"]
    baseline_result_id: UUID | None
    candidate_result_id: UUID | None
    improved_expectations: list[str] = Field(default_factory=list)
    regressed_expectations: list[str] = Field(default_factory=list)


class RunComparison(BaseModel):
    baseline_run_id: UUID
    candidate_run_id: UUID
    baseline: RunMetrics
    candidate: RunMetrics
    deltas: dict[str, MetricDelta]
    failure_category_deltas: dict[str, int]
    pricing_changed: bool
    identical_case_set: bool
    cases: list[CaseComparison]


def delta(before: int | float | Decimal | None, after: int | float | Decimal | None) -> MetricDelta:
    if before is None or after is None:
        return MetricDelta(absolute=None, percent=None)
    a, b = Decimal(str(before)), Decimal(str(after))
    with localcontext() as context:
        context.prec = 50
        return MetricDelta(absolute=b - a, percent=(b - a) / abs(a) * 100 if a else None)


def compare_case(before: CaseResult, after: CaseResult) -> CaseComparison:
    old = {s.scorer_name: s.passed for s in before.scoring_results}
    new = {s.scorer_name: s.passed for s in after.scoring_results}
    improved = sorted(name for name in old.keys() & new.keys() if not old[name] and new[name])
    regressed = sorted(name for name in old.keys() & new.keys() if old[name] and not new[name])
    old_ok, new_ok = before.status == CaseStatus.COMPLETED, after.status == CaseStatus.COMPLETED
    classification: Literal["improved", "regressed", "unchanged", "unmatched"] = "unchanged"
    if (old_ok and not new_ok) or regressed:
        classification = "regressed"
    elif (new_ok and not old_ok) or improved:
        classification = "improved"
    return CaseComparison(
        test_case_id=before.test_case_id,
        classification=classification,
        baseline_result_id=before.id,
        candidate_result_id=after.id,
        improved_expectations=improved,
        regressed_expectations=regressed,
    )


def validate_scores(run: EvaluationRun, results: list[CaseResult]) -> None:
    require_finished(run, results)
    if run.scoring_version is None or any(r.expectations_snapshot is None for r in results):
        raise ScoringConflict("Runs need historical expectation and scorer-version snapshots")
    for result in results:
        if result.status == CaseStatus.COMPLETED and result.expectations_snapshot:
            if outcome(result) == "not_scored":
                raise ScoringConflict("Score every eligible case before comparison")
        if any(s.scorer_version != run.scoring_version for s in result.scoring_results):
            raise ScoringConflict("Scorer versions do not match the run snapshot")


def compare_runs(
    session: Session, baseline: EvaluationRun, candidate: EvaluationRun
) -> RunComparison:
    if (baseline.project_id, baseline.dataset_id) != (candidate.project_id, candidate.dataset_id):
        raise ScoringConflict("Comparison requires the same project and dataset identity")
    if baseline.scoring_version != candidate.scoring_version:
        raise ScoringConflict("Scorer versions are incompatible")
    old_results, new_results = (
        results_for_run(session, baseline.id),
        results_for_run(session, candidate.id),
    )
    validate_scores(baseline, old_results)
    validate_scores(candidate, new_results)
    old = {r.test_case_id: r for r in old_results}
    new = {r.test_case_id: r for r in new_results}
    if not old.keys() & new.keys():
        raise ScoringConflict("Runs have no matching test cases")
    comparisons: list[CaseComparison] = []
    for case_id in sorted(old.keys() | new.keys()):
        a, b = old.get(case_id), new.get(case_id)
        if a is None or b is None:
            comparisons.append(
                CaseComparison(
                    test_case_id=case_id,
                    classification="unmatched",
                    baseline_result_id=a.id if a else None,
                    candidate_result_id=b.id if b else None,
                )
            )
            continue
        old_expectations: list[JsonValue] = list(a.expectations_snapshot or [])
        new_expectations: list[JsonValue] = list(b.expectations_snapshot or [])
        if not json_equal(old_expectations, new_expectations) or not json_equal(
            a.input_snapshot, b.input_snapshot
        ):
            raise ScoringConflict(
                "Matching cases must have identical inputs and expectation snapshots"
            )
        comparisons.append(compare_case(a, b))
    before, after = aggregate(old_results), aggregate(new_results)
    numeric_before, numeric_after = before.model_dump(), after.model_dump()
    deltas = {
        key: delta(value, numeric_after[key])
        for key, value in numeric_before.items()
        if key != "failure_category_counts"
    }
    categories = before.failure_category_counts.keys() | after.failure_category_counts.keys()
    return RunComparison(
        baseline_run_id=baseline.id,
        candidate_run_id=candidate.id,
        baseline=before,
        candidate=after,
        deltas=deltas,
        failure_category_deltas={
            key: after.failure_category_counts.get(key, 0)
            - before.failure_category_counts.get(key, 0)
            for key in sorted(categories)
        },
        pricing_changed=baseline.pricing_snapshot != candidate.pricing_snapshot,
        identical_case_set=old.keys() == new.keys(),
        cases=comparisons,
    )
