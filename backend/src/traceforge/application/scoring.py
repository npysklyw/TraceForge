"""Append-once scoring of persisted observations; never edits execution evidence."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from traceforge.domain.status import CaseStatus, RunStatus
from traceforge.persistence.models import CaseResult, EvaluationRun, ScoringResult
from traceforge.scoring.expectations import VERSION, Pricing, parse_expectations
from traceforge.scoring.scorers import Evidence, ToolEvidence, estimate_cost, score


class ScoringConflict(Exception):
    pass


def score_case(session: Session, result_id: UUID, run: EvaluationRun) -> None:
    result = session.scalar(
        select(CaseResult)
        .where(CaseResult.id == result_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if result is None:
        raise ScoringConflict("Result no longer exists")
    if result.run_id != run.id:
        raise ScoringConflict("Result does not belong to the specified run")
    if result.status != CaseStatus.COMPLETED:
        session.commit()
        return
    if run.scoring_version != VERSION or result.expectations_snapshot is None:
        raise ScoringConflict("Missing or unsupported scoring snapshot; create a new run")
    expectations = parse_expectations(result.expectations_snapshot)
    existing = list(
        session.scalars(select(ScoringResult).where(ScoringResult.case_result_id == result_id))
    )
    if result.scored_at is not None:
        if {(s.scorer_name, s.scorer_type, s.scorer_version) for s in existing} != {
            (e.name, e.type, VERSION) for e in expectations
        }:
            raise ScoringConflict("Existing scores do not match the scoring snapshot")
        session.commit()
        return
    if existing:
        raise ScoringConflict("Historical scores already exist; they will not be overwritten")
    pricing = Pricing.model_validate(run.pricing_snapshot) if run.pricing_snapshot else None
    result.estimated_cost_usd = estimate_cost(pricing, result.input_tokens, result.output_tokens)
    evidence = Evidence(
        output=result.response,
        latency_ms=result.latency_ms,
        cost_usd=result.estimated_cost_usd,
        tools=[ToolEvidence(t.name, t.arguments, t.arguments_validated) for t in result.tool_calls],
    )
    for expectation in expectations:
        verdict = score(expectation, evidence)
        session.add(
            ScoringResult(
                case_result_id=result.id,
                scorer_name=expectation.name,
                scorer_type=expectation.type,
                scorer_version=VERSION,
                value=float(verdict.passed),
                passed=verdict.passed,
                explanation=verdict.explanation,
                expected=verdict.expected,
                observed=verdict.observed,
                details={"category": verdict.category},
            )
        )
    result.scored_at = datetime.now(UTC)
    session.commit()  # One atomic batch per case, independently durable.
    session.expire(result, ["scoring_results"])


def results_for_run(session: Session, run_id: UUID) -> list[CaseResult]:
    return list(
        session.scalars(
            select(CaseResult)
            .where(CaseResult.run_id == run_id)
            .options(selectinload(CaseResult.scoring_results))
            .order_by(CaseResult.test_case_id)
            .execution_options(populate_existing=True)
        )
    )


def require_finished(run: EvaluationRun, results: list[CaseResult]) -> None:
    if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED} or run.finished_at is None:
        raise ScoringConflict("Run must have finished execution")
    if any(r.status not in {CaseStatus.COMPLETED, CaseStatus.ERROR} for r in results):
        raise ScoringConflict("Run contains unfinished or unsupported case statuses")


def score_run(session: Session, run: EvaluationRun) -> None:
    results = results_for_run(session, run.id)
    require_finished(run, results)
    if run.scoring_version != VERSION or any(r.expectations_snapshot is None for r in results):
        raise ScoringConflict("Missing or unsupported scoring snapshot; create a new run")
    for result in results:
        score_case(session, result.id, run)
