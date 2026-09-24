"""Seed original fictional support scenarios: python -m traceforge.demo [--execute]."""

import argparse
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceforge.application.evaluations import EvaluationService, create_run
from traceforge.persistence.database import get_engine
from traceforge.persistence.models import AgentConfiguration, EvaluationDataset, Project, TestCase
from traceforge.providers.fake import SCENARIOS


def seed_demo(session: Session) -> tuple[UUID, UUID, UUID]:
    name = "TraceForge fictional support demo"
    project = session.scalar(select(Project).where(Project.name == name))
    if project is not None:
        agent = session.scalar(
            select(AgentConfiguration).where(AgentConfiguration.project_id == project.id)
        )
        dataset = session.scalar(
            select(EvaluationDataset).where(EvaluationDataset.project_id == project.id)
        )
        if agent is None or dataset is None:
            raise ValueError(
                "Demo project is incomplete; use a fresh database or restore its records"
            )
        return project.id, agent.id, dataset.id
    project = Project(
        name=name, description="Original synthetic data; no real customers or orders."
    )
    session.add(project)
    session.flush()
    agent = AgentConfiguration(
        project_id=project.id,
        name="Support fake v1",
        provider="fake",
        model_name="deterministic-v1",
        parameters={"max_steps": 8},
        system_prompt="Help with fictional orders using the registered support tools.",
    )
    dataset = EvaluationDataset(project_id=project.id, name="Support scenarios v1")
    session.add_all([agent, dataset])
    session.flush()
    for scenario in SCENARIOS:
        session.add(
            TestCase(
                dataset_id=dataset.id,
                name=scenario,
                input={"scenario": scenario},
                expected_output=None,
            )
        )
    session.commit()
    return project.id, agent.id, dataset.id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Create and execute a new demo run")
    args = parser.parse_args()
    with Session(get_engine(), expire_on_commit=False) as session:
        project_id, agent_id, dataset_id = seed_demo(session)
        output = {
            "project_id": str(project_id),
            "agent_configuration_id": str(agent_id),
            "dataset_id": str(dataset_id),
        }
        if args.execute:
            run = create_run(session, project_id, agent_id, dataset_id)
            EvaluationService(session).execute(run.id)
            output.update(run_id=str(run.id), status=run.status.value)
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
