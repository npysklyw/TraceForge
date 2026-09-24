from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from traceforge.api import schemas as s
from traceforge.api.routes import DB, Limit, Offset, get_record, page
from traceforge.application.comparison import RunComparison, compare_runs
from traceforge.application.evaluations import (
    EvaluationService,
    ExecutionConflict,
    InvalidEvaluation,
    ResourceNotFound,
    case_counts,
    create_run,
)
from traceforge.application.scoring import ScoringConflict, results_for_run, score_run
from traceforge.persistence.models import CaseResult, EvaluationRun
from traceforge.scoring.metrics import RunMetrics, aggregate

router = APIRouter(tags=["evaluations"])


def run_detail(session: DB, run: EvaluationRun) -> s.RunDetail:
    counts = case_counts(session, run.id)
    return s.RunDetail(
        **s.EvaluationRunResponse.model_validate(run).model_dump(),
        case_counts=counts,
        total_cases=sum(counts.values()),
    )


@router.post("/runs", response_model=s.RunDetail, status_code=201)
def new_run(payload: s.EvaluationRunCreate, session: DB) -> s.RunDetail:
    try:
        run = create_run(
            session, payload.project_id, payload.agent_configuration_id, payload.dataset_id
        )
    except ResourceNotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except InvalidEvaluation as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    return run_detail(session, run)


@router.post("/runs/{run_id}/execute", response_model=s.RunDetail)
def execute_run(run_id: UUID, session: DB) -> s.RunDetail:
    try:
        run = EvaluationService(session).execute(run_id)
    except ResourceNotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except InvalidEvaluation as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except ExecutionConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    return run_detail(session, run)


@router.get("/runs/{run_id}", response_model=s.RunDetail)
def read_run(run_id: UUID, session: DB) -> s.RunDetail:
    return run_detail(session, get_record(session, EvaluationRun, run_id))


@router.get("/runs/{run_id}/results", response_model=s.Page[s.CaseResultResponse])
def list_results(
    run_id: UUID, session: DB, limit: Limit = 20, offset: Offset = 0
) -> s.Page[s.CaseResultResponse]:
    get_record(session, EvaluationRun, run_id)
    return page(
        session,
        select(CaseResult).where(CaseResult.run_id == run_id),
        s.CaseResultResponse,
        limit,
        offset,
    )


@router.get("/results/{result_id}", response_model=s.ResultDetail)
def read_result(result_id: UUID, session: DB) -> CaseResult:
    return get_record(session, CaseResult, result_id)


@router.post("/runs/{run_id}/score", response_model=RunMetrics)
def score_evaluation(run_id: UUID, session: DB) -> RunMetrics:
    run = get_record(session, EvaluationRun, run_id)
    try:
        score_run(session, run)
    except ScoringConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    return aggregate(results_for_run(session, run_id))


@router.get("/runs/{run_id}/metrics", response_model=RunMetrics)
def read_metrics(run_id: UUID, session: DB) -> RunMetrics:
    get_record(session, EvaluationRun, run_id)
    return aggregate(results_for_run(session, run_id))


@router.get("/runs/{baseline_id}/compare/{candidate_id}", response_model=RunComparison)
def compare_evaluations(baseline_id: UUID, candidate_id: UUID, session: DB) -> RunComparison:
    baseline = get_record(session, EvaluationRun, baseline_id)
    candidate = get_record(session, EvaluationRun, candidate_id)
    try:
        return compare_runs(session, baseline, candidate)
    except ScoringConflict as exc:
        raise HTTPException(409, str(exc)) from exc
