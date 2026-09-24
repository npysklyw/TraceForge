"""Short transactions around claims, observations, and case outcomes; no HTTP dependencies."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from traceforge.application.redaction import TraceSanitizer
from traceforge.application.runner import AgentRunner, ExecutionEventData, ToolObservation
from traceforge.application.scoring import score_case
from traceforge.domain.status import (
    CaseStatus,
    RunStatus,
    validate_case_transition,
    validate_run_transition,
)
from traceforge.persistence.models import (
    AgentConfiguration,
    CaseResult,
    EvaluationDataset,
    EvaluationRun,
    ExecutionEvent,
    Project,
    Record,
    TestCase,
    ToolCall,
)
from traceforge.providers.base import ModelProvider, ModelRequest
from traceforge.providers.fake import FakeModelProvider
from traceforge.scoring.expectations import VERSION, Pricing, parse_expectations
from traceforge.tools.registry import ToolRegistry
from traceforge.tools.support import support_registry


class ResourceNotFound(Exception):
    pass


class ExecutionConflict(Exception):
    pass


class InvalidEvaluation(Exception):
    pass


def require[T: Record](
    session: Session, model: type[T], record_id: UUID, *, lock: bool = False
) -> T:
    query = select(model).where(model.id == record_id)
    if lock:
        query = query.with_for_update()
    record = session.scalar(query.execution_options(populate_existing=True))
    if record is None:
        raise ResourceNotFound(f"{model.__name__} not found")
    return record


def max_steps(agent: AgentConfiguration) -> int:
    value = agent.parameters.get("max_steps", 8)
    if type(value) is not int or not 1 <= value <= 12:
        raise InvalidEvaluation("max_steps must be an integer between 1 and 12")
    return value


def validate_agent(agent: AgentConfiguration) -> None:
    if agent.provider != "fake":
        raise InvalidEvaluation("Only the offline fake provider is executable in this milestone")
    max_steps(agent)


def create_run(
    session: Session, project_id: UUID, agent_id: UUID, dataset_id: UUID
) -> EvaluationRun:
    require(session, Project, project_id)
    # Lock the same parents as CRUD before creating the references that freeze them.
    agent = require(session, AgentConfiguration, agent_id, lock=True)
    dataset = require(session, EvaluationDataset, dataset_id, lock=True)
    if agent.project_id != project_id or dataset.project_id != project_id:
        raise InvalidEvaluation("Configuration and dataset must belong to the specified project")
    validate_agent(agent)
    cases = list(
        session.scalars(
            select(TestCase)
            .where(TestCase.dataset_id == dataset.id)
            .order_by(TestCase.created_at, TestCase.id)
        )
    )
    if not cases:
        raise InvalidEvaluation("The dataset must contain at least one test case")
    if len(cases) > 1000:
        raise InvalidEvaluation("Synchronous evaluations support at most 1000 cases")
    try:
        if agent.pricing is not None:
            Pricing.model_validate(agent.pricing)
        snapshots = {
            case.id: [
                item.model_dump(mode="json") for item in parse_expectations(case.expectations)
            ]
            for case in cases
        }
        for snapshot in snapshots.values():
            for item in snapshot:
                if TraceSanitizer(item).clean(item) != item:
                    raise InvalidEvaluation("Expectations cannot contain secrets or reasoning")
    except ValidationError as exc:
        raise InvalidEvaluation("Invalid expectations or pricing configuration") from exc
    run = EvaluationRun(
        project_id=project_id,
        agent_configuration_id=agent_id,
        dataset_id=dataset_id,
        pricing_snapshot=deepcopy(agent.pricing),
        scoring_version=VERSION,
    )
    session.add(run)
    session.flush()
    for case in cases:
        sanitizer = TraceSanitizer(case.input, agent.parameters, agent.system_prompt)
        session.add(
            CaseResult(
                run_id=run.id,
                dataset_id=dataset_id,
                test_case_id=case.id,
                input_snapshot=sanitizer.object(case.input),
                expectations_snapshot=snapshots[case.id],
                provider=agent.provider,
                model_name=sanitizer.text(agent.model_name)[:200],
            )
        )
    session.commit()
    return run


class TraceRecorder:
    def __init__(self, session: Session, result_id: UUID) -> None:
        self.session = session
        self.result_id = result_id
        self.sequence = 0

    def event(self, event: ExecutionEventData) -> None:
        self.session.add(
            ExecutionEvent(
                case_result_id=self.result_id,
                sequence=self.sequence,
                kind=event.kind,
                payload=event.payload,
                started_at=event.started_at,
                finished_at=event.finished_at,
                latency_ms=event.latency_ms,
                **event.usage.model_dump(),
            )
        )
        self.session.commit()
        self.sequence += 1

    def tool(self, tool: ToolObservation) -> None:
        self.session.add(
            ToolCall(
                case_result_id=self.result_id,
                sequence=tool.sequence,
                provider_call_id=tool.call_id,
                name=tool.name,
                arguments=tool.arguments,
                arguments_validated=tool.arguments_validated,
                output=tool.output,
                error_type=tool.error_type,
                error=tool.error,
                started_at=tool.started_at,
                finished_at=tool.finished_at,
                latency_ms=tool.latency_ms,
            )
        )
        self.session.commit()


class EvaluationService:
    def __init__(
        self,
        session: Session,
        provider: ModelProvider | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.session = session
        self.runner = AgentRunner(provider or FakeModelProvider(), registry or support_registry())

    def execute(self, run_id: UUID) -> EvaluationRun:
        session = self.session
        run = require(session, EvaluationRun, run_id, lock=True)
        if run.status != RunStatus.PENDING:
            raise ExecutionConflict("Only pending runs can execute; create a new run to retry")
        agent = require(session, AgentConfiguration, run.agent_configuration_id)
        dataset = require(session, EvaluationDataset, run.dataset_id)
        require(session, Project, run.project_id)
        validate_agent(agent)
        if agent.project_id != run.project_id or dataset.project_id != run.project_id:
            raise InvalidEvaluation("Run references are inconsistent")
        results = list(
            session.scalars(
                select(CaseResult)
                .join(TestCase, TestCase.id == CaseResult.test_case_id)
                .where(CaseResult.run_id == run.id)
                .order_by(TestCase.created_at, TestCase.id)
            )
        )
        case_ids = set(
            session.scalars(select(TestCase.id).where(TestCase.dataset_id == run.dataset_id))
        )
        if not results or {result.test_case_id for result in results} != case_ids:
            raise InvalidEvaluation("Run cases do not match the dataset; create a new run")
        if any(result.status != CaseStatus.PENDING for result in results):
            raise ExecutionConflict("Run contains cases that already started")
        validate_run_transition(run.status, RunStatus.RUNNING)
        run.status, run.started_at = RunStatus.RUNNING, datetime.now(UTC)
        session.commit()  # The claim is visible before any provider/tool invocation.

        for result in results:
            case = require(session, TestCase, result.test_case_id)
            sanitizer = TraceSanitizer(case.input, agent.parameters, agent.system_prompt)
            request = ModelRequest(
                model=agent.model_name,
                system_prompt=agent.system_prompt,
                parameters=agent.parameters,
                input=case.input,
            )
            validate_case_transition(result.status, CaseStatus.RUNNING)
            result.status, result.started_at = CaseStatus.RUNNING, datetime.now(UTC)
            result.input_snapshot = sanitizer.object(case.input)
            result.provider, result.model_name = (
                agent.provider,
                sanitizer.text(agent.model_name)[:200],
            )
            session.commit()  # No transaction remains open while the model or tool runs.
            recorder = TraceRecorder(session, result.id)
            outcome = self.runner.run(
                request,
                max_steps=max_steps(agent),
                sanitizer=sanitizer,
                on_event=recorder.event,
                on_tool=recorder.tool,
            )
            target = CaseStatus.ERROR if outcome.error_type else CaseStatus.COMPLETED
            validate_case_transition(result.status, target)
            result.status, result.response = target, outcome.output
            result.error_type, result.error = outcome.error_type, outcome.error
            result.started_at, result.finished_at = outcome.started_at, outcome.finished_at
            result.latency_ms = outcome.latency_ms
            result.input_tokens = outcome.usage.input_tokens
            result.output_tokens = outcome.usage.output_tokens
            result.total_tokens = outcome.usage.total_tokens
            session.commit()  # Each outcome survives a later case failure.
            session.expire(result, ["events", "tool_calls"])
            if run.scoring_version is not None:
                score_case(session, result.id, run)

        counts = case_counts(session, run.id)
        target_run = RunStatus.FAILED if counts.get("error", 0) else RunStatus.COMPLETED
        validate_run_transition(run.status, target_run)
        run.status, run.finished_at = target_run, datetime.now(UTC)
        run.error = (
            "One or more cases encountered an execution error." if counts.get("error") else None
        )
        session.commit()
        return run


def case_counts(session: Session, run_id: UUID) -> dict[str, int]:
    counts = {status.value: 0 for status in CaseStatus}
    for status, count in session.execute(
        select(CaseResult.status, func.count())
        .where(CaseResult.run_id == run_id)
        .group_by(CaseResult.status)
    ):
        counts[status.value] = count
    return counts
