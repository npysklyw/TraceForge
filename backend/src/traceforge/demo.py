"""Seed original fictional support scenarios: python -m traceforge.demo [--execute]."""

import argparse
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from traceforge.application.evaluations import EvaluationService, create_run
from traceforge.persistence.database import get_engine
from traceforge.persistence.models import AgentConfiguration, EvaluationDataset, Project, TestCase
from traceforge.providers.base import ModelResponse
from traceforge.providers.fake import FINAL, SCENARIOS


def seed_demo(session: Session, *, scoring: bool = False) -> tuple[UUID, UUID, UUID]:
    name = (
        "TraceForge fictional support scoring demo"
        if scoring
        else "TraceForge fictional support demo"
    )
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
        pricing={"input_usd_per_million": "1", "output_usd_per_million": "2"} if scoring else None,
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
                expectations=[
                    {"name": "answer", "type": "exact_output", "expected": FINAL.text},
                    {
                        "name": "tools",
                        "type": "tool_selection",
                        "expected": [
                            action.tool_call.name
                            for action in SCENARIOS[scenario]
                            if isinstance(action, ModelResponse) and action.tool_call is not None
                        ],
                    },
                ]
                if scoring
                else [],
            )
        )
    session.commit()
    return project.id, agent.id, dataset.id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Create and execute a new demo run")
    parser.add_argument("--scoring", action="store_true", help="Use the versioned scoring demo")
    args = parser.parse_args()
    with Session(get_engine(), expire_on_commit=False) as session:
        project_id, agent_id, dataset_id = seed_demo(session, scoring=args.scoring)
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
