"""Small original fixtures for offline PR gates; never edits previous experiments."""

from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.orm import Session

from benchwarden.persistence.models import AgentConfiguration, EvaluationDataset, Project, TestCase
from benchwarden.providers.fake import FINAL


class SeedIds(BaseModel):
    project_id: UUID
    dataset_id: UUID
    baseline_config_id: UUID
    candidate_config_id: UUID


def seed_ci(session: Session, *, regression: bool = False) -> SeedIds:
    project = Project(name=f"Benchwarden offline CI {uuid4()}")
    session.add(project)
    session.flush()
    dataset = EvaluationDataset(project_id=project.id, name="Synthetic support gate v1")
    baseline = AgentConfiguration(
        project_id=project.id,
        name="baseline",
        model_name="deterministic-v1",
        parameters={"fake_variant": "standard"},
        pricing={"input_usd_per_million": "1", "output_usd_per_million": "2"},
    )
    candidate = AgentConfiguration(
        project_id=project.id,
        name="candidate",
        model_name="deterministic-v1",
        parameters={"fake_variant": "wrong_answer" if regression else "standard"},
        pricing={"input_usd_per_million": "1", "output_usd_per_million": "2"},
    )
    session.add_all([dataset, baseline, candidate])
    session.flush()
    for scenario, tools in [("text_only", []), ("valid_tool", ["get_order"])]:
        session.add(
            TestCase(
                dataset_id=dataset.id,
                name=scenario,
                input={"scenario": scenario},
                expected_output=FINAL.text,
                expectations=[
                    {"name": "answer", "type": "exact_output", "expected": FINAL.text},
                    {"name": "tools", "type": "tool_selection", "expected": tools},
                ],
            )
        )
    session.commit()
    return SeedIds(
        project_id=project.id,
        dataset_id=dataset.id,
        baseline_config_id=baseline.id,
        candidate_config_id=candidate.id,
    )
