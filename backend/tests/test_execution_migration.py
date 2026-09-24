from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from conftest import alembic_config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from traceforge.domain.status import CaseStatus
from traceforge.persistence.models import (
    AgentConfiguration,
    CaseResult,
    EvaluationDataset,
    EvaluationRun,
    ExecutionEvent,
    Project,
)
from traceforge.persistence.models import (
    TestCase as Case,
)

pytestmark = pytest.mark.integration


def test_populated_trace_migration_downgrade_and_reapply(database_engine):
    with database_engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(
                connection, join_transaction_mode="create_savepoint", expire_on_commit=False
            ) as session:
                project = Project(name=f"Migration {uuid4()}")
                session.add(project)
                session.flush()
                agent = AgentConfiguration(project_id=project.id, name="fake", model_name="fake")
                dataset = EvaluationDataset(project_id=project.id, name="migration")
                session.add_all([agent, dataset])
                session.flush()
                case = Case(dataset_id=dataset.id, name="case", input={}, expected_output=None)
                run = EvaluationRun(
                    project_id=project.id, agent_configuration_id=agent.id, dataset_id=dataset.id
                )
                session.add_all([case, run])
                session.flush()
                result = CaseResult(
                    run_id=run.id,
                    test_case_id=case.id,
                    dataset_id=dataset.id,
                    status=CaseStatus.COMPLETED,
                    response="Observable final output",
                )
                session.add(result)
                session.flush()
                now = datetime.now(UTC)
                session.add(
                    ExecutionEvent(
                        case_result_id=result.id,
                        sequence=0,
                        kind="case_completed",
                        payload={"status": "completed"},
                        started_at=now,
                        finished_at=now,
                        latency_ms=0,
                    )
                )
                session.commit()
                result_id = result.id
            config = alembic_config(connection)
            command.downgrade(config, "0b3e65b38568")
            assert "execution_events" not in inspect(connection).get_table_names()
            row = connection.execute(
                text("SELECT status::text, response FROM case_results WHERE id=:id"),
                {"id": result_id},
            ).one()
            assert row == ("passed", "Observable final output")
            command.upgrade(config, "head")
            assert "execution_events" in inspect(connection).get_table_names()
            assert (
                connection.scalar(
                    text("SELECT status::text FROM case_results WHERE id=:id"), {"id": result_id}
                )
                == "passed"
            )
            command.check(config)
        finally:
            transaction.rollback()
