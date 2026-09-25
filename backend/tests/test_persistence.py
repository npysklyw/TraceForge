from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import alembic_config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError

from benchwarden.api.schemas import (
    CaseResultResponse,
    EvaluationRunResponse,
    ScoringResultResponse,
    ToolCallResponse,
)
from benchwarden.domain.status import CaseStatus, RunStatus
from benchwarden.persistence.models import (
    AgentConfiguration,
    Base,
    CaseResult,
    EvaluationDataset,
    EvaluationRun,
    Project,
    ScoringResult,
    ToolCall,
)
from benchwarden.persistence.models import (
    TestCase as EvaluationCase,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def run(session, records):
    run = EvaluationRun(
        project_id=UUID(records["project"]["id"]),
        agent_configuration_id=UUID(records["agent"]["id"]),
        dataset_id=UUID(records["dataset"]["id"]),
    )
    session.add(run)
    session.flush()
    return run


@pytest.fixture
def result(session, run, records):
    result = CaseResult(
        run_id=run.id, dataset_id=run.dataset_id, test_case_id=UUID(records["case"]["id"])
    )
    session.add(result)
    session.flush()
    return result


def test_migration_roundtrip_and_metadata_match(database_engine):
    with database_engine.begin() as connection:
        config = alembic_config(connection)
        assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {
            "alembic_version"
        }
        command.check(config)
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        assert not inspect(connection).get_enums()
        command.upgrade(config, "head")
        command.check(config)


def test_entire_record_graph_roundtrips(session, records, run, result):
    tool = ToolCall(
        case_result_id=result.id,
        sequence=0,
        name="add",
        provider_call_id="call-1",
        arguments={"numbers": [2, 3]},
        output=5,
    )
    score = ScoringResult(
        case_result_id=result.id,
        scorer_name="exact_match",
        scorer_version="1",
        value=1,
        passed=True,
        details={"expected": 5},
    )
    session.add_all([tool, score])
    session.commit()
    session.expire_all()
    assert run.status == RunStatus.PENDING
    assert result.status == CaseStatus.PENDING
    assert run.case_results == [result]
    assert result.tool_calls == [tool]
    assert result.scoring_results == [score]
    assert tool.arguments == {"numbers": [2, 3]}
    assert tool.output == 5
    assert score.value == 1
    for record, schema in [
        (run, EvaluationRunResponse),
        (result, CaseResultResponse),
        (tool, ToolCallResponse),
        (score, ScoringResultResponse),
    ]:
        assert isinstance(record.id, UUID)
        assert record.created_at.utcoffset() == timedelta(0)
        assert schema.model_validate(record).id == record.id
    project = session.get(Project, UUID(records["project"]["id"]))
    assert project.agent_configurations[0].id == run.agent_configuration_id
    assert project.datasets[0].test_cases[0].id == result.test_case_id


def test_run_cannot_mix_projects(session, run):
    other = Project(name="other")
    session.add(other)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            EvaluationRun(
                project_id=other.id,
                agent_configuration_id=run.agent_configuration_id,
                dataset_id=run.dataset_id,
            )
        )
        session.flush()


def test_case_result_must_belong_to_runs_dataset(session, run):
    other = EvaluationDataset(project_id=run.project_id, name="other")
    session.add(other)
    session.flush()
    case = EvaluationCase(dataset_id=other.id, name="other case", input={}, expected_output=None)
    session.add(case)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(CaseResult(run_id=run.id, dataset_id=run.dataset_id, test_case_id=case.id))
        session.flush()


def test_duplicate_result_rejected(session, result):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CaseResult(
                run_id=result.run_id, dataset_id=result.dataset_id, test_case_id=result.test_case_id
            )
        )
        session.flush()


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_database_enforces_score_bounds(session, result, value):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ScoringResult(
                case_result_id=result.id,
                scorer_name="exact",
                scorer_version="1",
                value=value,
                passed=False,
            )
        )
        session.flush()


def test_tool_order_and_scorer_uniqueness(session, result):
    for sequence in (2, 0, 1):
        session.add(ToolCall(case_result_id=result.id, sequence=sequence, name="add", arguments={}))
    session.add(
        ScoringResult(
            case_result_id=result.id, scorer_name="exact", scorer_version="1", value=1, passed=True
        )
    )
    session.flush()
    assert [tool.sequence for tool in result.tool_calls] == [0, 1, 2]
    for sequence in (-1, 0):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                ToolCall(case_result_id=result.id, sequence=sequence, name="add", arguments={})
            )
            session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ScoringResult(
                case_result_id=result.id,
                scorer_name="exact",
                scorer_version="1",
                value=0,
                passed=False,
            )
        )
        session.flush()


def test_unknown_parent_rejected_by_database(session):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(AgentConfiguration(project_id=uuid4(), name="orphan", model_name="fake"))
        session.flush()


def test_database_enforces_enum_values_and_time_order(session, run):
    with pytest.raises(DataError) as exc, session.begin_nested():
        session.execute(
            text("UPDATE evaluation_runs SET status = 'unknown' WHERE id = :id"), {"id": run.id}
        )
    assert "run_status" in str(exc.value)
    with pytest.raises(IntegrityError), session.begin_nested():
        run.started_at = datetime.now(UTC)
        run.finished_at = run.started_at - timedelta(seconds=1)
        session.flush()


def test_pending_run_can_be_cancelled_without_start_time(session, run):
    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(UTC)
    session.flush()
    assert run.started_at is None


def test_jsonb_type_and_null_storage(session, records):
    case_id = UUID(records["case"]["id"])
    case = session.get(EvaluationCase, case_id)
    case.expected_output = None
    session.flush()
    assert (
        session.scalar(
            text("SELECT jsonb_typeof(expected_output) FROM test_cases WHERE id = :id"),
            {"id": case_id},
        )
        == "null"
    )
    assert session.scalar(select(EvaluationCase.input).where(EvaluationCase.id == case_id)) == {
        "numbers": [2, 3]
    }
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text("UPDATE test_cases SET input = '[]'::jsonb WHERE id = :id"), {"id": case_id}
        )
