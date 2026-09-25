from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from conftest import alembic_config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from benchwarden.application.evaluations import EvaluationService, create_run
from benchwarden.persistence.models import AgentConfiguration, EvaluationDataset, Project
from benchwarden.persistence.models import TestCase as Case
from benchwarden.providers.fake import FINAL

pytestmark = pytest.mark.integration


def test_populated_scoring_downgrade_reapply_preserves_legacy_records(database_engine):
    with database_engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(
                connection, join_transaction_mode="create_savepoint", expire_on_commit=False
            ) as session:
                project = Project(name=f"Scoring migration {uuid4()}")
                session.add(project)
                session.flush()
                agent = AgentConfiguration(
                    project_id=project.id,
                    name="fake",
                    model_name="fake",
                    pricing={
                        "input_usd_per_million": "0.123456789012",
                        "output_usd_per_million": "0.2",
                    },
                )
                dataset = EvaluationDataset(project_id=project.id, name="scoring")
                session.add_all([agent, dataset])
                session.flush()
                case = Case(
                    dataset_id=dataset.id,
                    name="case",
                    input={"scenario": "text_only"},
                    expected_output=None,
                    expectations=[
                        {"name": "answer", "type": "exact_output", "expected": FINAL.text}
                    ],
                )
                session.add(case)
                session.commit()
                run = create_run(session, project.id, agent.id, dataset.id)
                EvaluationService(session).execute(run.id)
                result = run.case_results[0]
                result_id, score_id = result.id, result.scoring_results[0].id
                session.expire(result)
                assert result.estimated_cost_usd == Decimal("0.000002681481468144")
                trace = connection.execute(
                    text(
                        "SELECT id, payload, started_at FROM execution_events "
                        "WHERE case_result_id=:id ORDER BY sequence"
                    ),
                    {"id": result_id},
                ).all()
            config = alembic_config(connection)
            command.downgrade(config, "0fe7f845cf6f")
            assert "expectations" not in {
                c["name"] for c in inspect(connection).get_columns("test_cases")
            }
            assert connection.execute(
                text(
                    "SELECT scorer_name, scorer_version, passed, value "
                    "FROM scoring_results WHERE id=:id"
                ),
                {"id": score_id},
            ).one() == ("answer", "1", True, 1)
            assert (
                connection.execute(
                    text(
                        "SELECT id, payload, started_at FROM execution_events "
                        "WHERE case_result_id=:id ORDER BY sequence"
                    ),
                    {"id": result_id},
                ).all()
                == trace
            )
            command.upgrade(config, "head")
            assert connection.execute(
                text("SELECT expectations_snapshot, scored_at FROM case_results WHERE id=:id"),
                {"id": result_id},
            ).one() == (None, None)
            assert (
                connection.scalar(
                    text("SELECT scorer_type FROM scoring_results WHERE id=:id"), {"id": score_id}
                )
                == "legacy"
            )
            assert (
                connection.scalar(
                    text("SELECT expectations FROM test_cases WHERE id=:id"), {"id": case.id}
                )
                == []
            )
            command.check(config)
        finally:
            transaction.rollback()
