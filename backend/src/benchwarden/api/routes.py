"""Explicit CRUD routes with small shared HTTP/transaction helpers."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from benchwarden.api import schemas as s
from benchwarden.persistence.database import get_session
from benchwarden.persistence.models import (
    AgentConfiguration,
    EvaluationDataset,
    EvaluationRun,
    Project,
    Record,
    TestCase,
)

router = APIRouter()
DB = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=2147483647)]


def get_record[T: Record](
    session: Session, model: type[T], record_id: UUID, *, lock: bool = False
) -> T:
    query = select(model).where(model.id == record_id)
    if lock:
        query = query.with_for_update()
    record = session.scalar(query)
    if record is None:
        raise HTTPException(404, detail=f"{model.__name__} not found")
    return record


def page[T: Record, R: s.RecordResponse](
    session: Session, query: Select[tuple[T]], schema: type[R], limit: int, offset: int
) -> s.Page[R]:
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    model = query.column_descriptions[0]["entity"]
    records = session.scalars(
        query.order_by(model.created_at, model.id).limit(limit).offset(offset)
    )
    return s.Page(
        items=[schema.model_validate(record) for record in records],
        total=total,
        limit=limit,
        offset=offset,
    )


def commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise HTTPException(
                409, detail="A record with that name already exists in this scope"
            ) from exc
        if code == "23503":
            raise HTTPException(
                409, detail="Record is referenced by other records; remove children first"
            ) from exc
        raise


def update(record: Record, payload: s.Patch) -> None:
    for field, value in payload.model_dump(exclude_unset=True, mode="json").items():
        setattr(record, field, value)


def ensure_agent_editable(session: Session, agent_id: UUID) -> None:
    if session.scalar(
        select(EvaluationRun.id).where(EvaluationRun.agent_configuration_id == agent_id).limit(1)
    ):
        raise HTTPException(
            409, detail="Configuration is used by a run; create a new configuration"
        )


def editable_dataset(session: Session, dataset_id: UUID) -> EvaluationDataset:
    dataset = get_record(session, EvaluationDataset, dataset_id, lock=True)
    if session.scalar(
        select(EvaluationRun.id).where(EvaluationRun.dataset_id == dataset_id).limit(1)
    ):
        raise HTTPException(409, detail="Dataset is used by a run; create a new dataset")
    return dataset


@router.post("/projects", response_model=s.ProjectResponse, status_code=201, tags=["projects"])
def create_project(payload: s.ProjectCreate, session: DB) -> Project:
    record = Project(**payload.model_dump(mode="json"))
    session.add(record)
    commit(session)
    return record


@router.get("/projects", response_model=s.Page[s.ProjectResponse], tags=["projects"])
def list_projects(session: DB, limit: Limit = 20, offset: Offset = 0) -> s.Page[s.ProjectResponse]:
    return page(session, select(Project), s.ProjectResponse, limit, offset)


@router.get("/projects/{project_id}", response_model=s.ProjectResponse, tags=["projects"])
def read_project(project_id: UUID, session: DB) -> Project:
    return get_record(session, Project, project_id)


@router.patch("/projects/{project_id}", response_model=s.ProjectResponse, tags=["projects"])
def update_project(project_id: UUID, payload: s.ProjectUpdate, session: DB) -> Project:
    record = get_record(session, Project, project_id, lock=True)
    update(record, payload)
    commit(session)
    return record


@router.delete("/projects/{project_id}", status_code=204, tags=["projects"])
def delete_project(project_id: UUID, session: DB) -> Response:
    session.delete(get_record(session, Project, project_id, lock=True))
    commit(session)
    return Response(status_code=204)


@router.post(
    "/projects/{project_id}/agent-configurations",
    response_model=s.AgentConfigurationResponse,
    status_code=201,
    tags=["agent-configurations"],
)
def create_agent(
    project_id: UUID, payload: s.AgentConfigurationCreate, session: DB
) -> AgentConfiguration:
    get_record(session, Project, project_id)
    record = AgentConfiguration(project_id=project_id, **payload.model_dump(mode="json"))
    session.add(record)
    commit(session)
    return record


@router.get(
    "/projects/{project_id}/agent-configurations",
    response_model=s.Page[s.AgentConfigurationResponse],
    tags=["agent-configurations"],
)
def list_agents(
    project_id: UUID, session: DB, limit: Limit = 20, offset: Offset = 0
) -> s.Page[s.AgentConfigurationResponse]:
    get_record(session, Project, project_id)
    return page(
        session,
        select(AgentConfiguration).where(AgentConfiguration.project_id == project_id),
        s.AgentConfigurationResponse,
        limit,
        offset,
    )


@router.get(
    "/agent-configurations/{agent_id}",
    response_model=s.AgentConfigurationResponse,
    tags=["agent-configurations"],
)
def read_agent(agent_id: UUID, session: DB) -> AgentConfiguration:
    return get_record(session, AgentConfiguration, agent_id)


@router.patch(
    "/agent-configurations/{agent_id}",
    response_model=s.AgentConfigurationResponse,
    tags=["agent-configurations"],
)
def update_agent(
    agent_id: UUID, payload: s.AgentConfigurationUpdate, session: DB
) -> AgentConfiguration:
    record = get_record(session, AgentConfiguration, agent_id, lock=True)
    ensure_agent_editable(session, agent_id)
    update(record, payload)
    commit(session)
    return record


@router.delete("/agent-configurations/{agent_id}", status_code=204, tags=["agent-configurations"])
def delete_agent(agent_id: UUID, session: DB) -> Response:
    record = get_record(session, AgentConfiguration, agent_id, lock=True)
    ensure_agent_editable(session, agent_id)
    session.delete(record)
    commit(session)
    return Response(status_code=204)


@router.post(
    "/projects/{project_id}/datasets",
    response_model=s.DatasetResponse,
    status_code=201,
    tags=["datasets"],
)
def create_dataset(project_id: UUID, payload: s.DatasetCreate, session: DB) -> EvaluationDataset:
    get_record(session, Project, project_id)
    record = EvaluationDataset(project_id=project_id, **payload.model_dump(mode="json"))
    session.add(record)
    commit(session)
    return record


@router.get(
    "/projects/{project_id}/datasets", response_model=s.Page[s.DatasetResponse], tags=["datasets"]
)
def list_datasets(
    project_id: UUID, session: DB, limit: Limit = 20, offset: Offset = 0
) -> s.Page[s.DatasetResponse]:
    get_record(session, Project, project_id)
    return page(
        session,
        select(EvaluationDataset).where(EvaluationDataset.project_id == project_id),
        s.DatasetResponse,
        limit,
        offset,
    )


@router.get("/datasets/{dataset_id}", response_model=s.DatasetResponse, tags=["datasets"])
def read_dataset(dataset_id: UUID, session: DB) -> EvaluationDataset:
    return get_record(session, EvaluationDataset, dataset_id)


@router.patch("/datasets/{dataset_id}", response_model=s.DatasetResponse, tags=["datasets"])
def update_dataset(dataset_id: UUID, payload: s.DatasetUpdate, session: DB) -> EvaluationDataset:
    record = editable_dataset(session, dataset_id)
    update(record, payload)
    commit(session)
    return record


@router.delete("/datasets/{dataset_id}", status_code=204, tags=["datasets"])
def delete_dataset(dataset_id: UUID, session: DB) -> Response:
    session.delete(editable_dataset(session, dataset_id))
    commit(session)
    return Response(status_code=204)


@router.post(
    "/datasets/{dataset_id}/test-cases",
    response_model=s.TestCaseResponse,
    status_code=201,
    tags=["test-cases"],
)
def create_case(dataset_id: UUID, payload: s.TestCaseCreate, session: DB) -> TestCase:
    editable_dataset(session, dataset_id)
    record = TestCase(dataset_id=dataset_id, **payload.model_dump(mode="json"))
    session.add(record)
    commit(session)
    return record


@router.get(
    "/datasets/{dataset_id}/test-cases",
    response_model=s.Page[s.TestCaseResponse],
    tags=["test-cases"],
)
def list_cases(
    dataset_id: UUID, session: DB, limit: Limit = 20, offset: Offset = 0
) -> s.Page[s.TestCaseResponse]:
    get_record(session, EvaluationDataset, dataset_id)
    return page(
        session,
        select(TestCase).where(TestCase.dataset_id == dataset_id),
        s.TestCaseResponse,
        limit,
        offset,
    )


@router.get("/test-cases/{case_id}", response_model=s.TestCaseResponse, tags=["test-cases"])
def read_case(case_id: UUID, session: DB) -> TestCase:
    return get_record(session, TestCase, case_id)


@router.patch("/test-cases/{case_id}", response_model=s.TestCaseResponse, tags=["test-cases"])
def update_case(case_id: UUID, payload: s.TestCaseUpdate, session: DB) -> TestCase:
    record = get_record(session, TestCase, case_id, lock=True)
    editable_dataset(session, record.dataset_id)
    update(record, payload)
    commit(session)
    return record


@router.delete("/test-cases/{case_id}", status_code=204, tags=["test-cases"])
def delete_case(case_id: UUID, session: DB) -> Response:
    record = get_record(session, TestCase, case_id, lock=True)
    editable_dataset(session, record.dataset_id)
    session.delete(record)
    commit(session)
    return Response(status_code=204)
