from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from traceforge.api import schemas as s
from traceforge.api.routes import DB, Limit, Offset, get_record, page
from traceforge.application.evaluations import (
    EvaluationService,
    ExecutionConflict,
    InvalidEvaluation,
    ResourceNotFound,
    case_counts,
    create_run,
)
from traceforge.persistence.models import CaseResult, EvaluationRun

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
