"""Committed PostgreSQL smoke test, including concurrent repeat scoring."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from benchwarden.application.comparison import compare_runs
from benchwarden.application.evaluations import EvaluationService, create_run
from benchwarden.application.scoring import score_run
from benchwarden.persistence.models import (
    AgentConfiguration,
    CaseResult,
    EvaluationDataset,
    EvaluationRun,
    ExecutionEvent,
    Project,
    ScoringResult,
    ToolCall,
)
from benchwarden.persistence.models import TestCase as Case
from benchwarden.providers.fake import FINAL

pytestmark = pytest.mark.integration


def test_postgresql_scoring_comparison_smoke_and_concurrent_idempotency(database_engine):
    with Session(database_engine, expire_on_commit=False) as session:
        project = Project(name=f"Scoring smoke {uuid4()}")
        session.add(project)
        session.flush()
        project_id = project.id
        agent = AgentConfiguration(
            project_id=project.id,
            name="fake",
            model_name="fake",
            pricing={"input_usd_per_million": "1", "output_usd_per_million": "2"},
        )
        dataset = EvaluationDataset(project_id=project.id, name="smoke")
        session.add_all([agent, dataset])
        session.flush()
        session.add(
            Case(
                dataset_id=dataset.id,
                name="text",
                input={"scenario": "text_only"},
                expected_output=FINAL.text,
                expectations=[{"name": "answer", "type": "exact_output", "expected": FINAL.text}],
            )
        )
        session.commit()
        baseline = create_run(session, project.id, agent.id, dataset.id)
        candidate = create_run(session, project.id, agent.id, dataset.id)
        baseline_id, candidate_id = baseline.id, candidate.id
        EvaluationService(session).execute(baseline_id)
        EvaluationService(session).execute(candidate_id)
        # Simulate the small recovery window after execution was committed, before scoring.
        result = session.scalar(select(CaseResult).where(CaseResult.run_id == candidate_id))
        session.execute(delete(ScoringResult).where(ScoringResult.case_result_id == result.id))
        result.scored_at = None
        session.commit()

    def rescore():
        with Session(database_engine, expire_on_commit=False) as session:
            score_run(session, session.get(EvaluationRun, candidate_id))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(rescore) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        with Session(database_engine) as verify:
            comparison = compare_runs(
                verify,
                verify.get(EvaluationRun, baseline_id),
                verify.get(EvaluationRun, candidate_id),
            )
            assert comparison.baseline.pass_rate == comparison.candidate.pass_rate == 1
            assert comparison.candidate.total_tokens == 18
            assert comparison.cases[0].classification == "unchanged"
            result = verify.scalar(select(CaseResult).where(CaseResult.run_id == candidate_id))
            assert len(result.scoring_results) == 1
            assert result.events[-1].kind == "case_completed"
    finally:
        with Session(database_engine) as cleanup:
            run_ids = select(EvaluationRun.id).where(EvaluationRun.project_id == project_id)
            result_ids = select(CaseResult.id).where(CaseResult.run_id.in_(run_ids))
            for model in (ExecutionEvent, ToolCall, ScoringResult):
                cleanup.execute(delete(model).where(model.case_result_id.in_(result_ids)))
            cleanup.execute(delete(CaseResult).where(CaseResult.run_id.in_(run_ids)))
            cleanup.execute(delete(EvaluationRun).where(EvaluationRun.project_id == project_id))
            dataset_ids = select(EvaluationDataset.id).where(
                EvaluationDataset.project_id == project_id
            )
            cleanup.execute(delete(Case).where(Case.dataset_id.in_(dataset_ids)))
            cleanup.execute(
                delete(EvaluationDataset).where(EvaluationDataset.project_id == project_id)
            )
            cleanup.execute(
                delete(AgentConfiguration).where(AgentConfiguration.project_id == project_id)
            )
            cleanup.execute(delete(Project).where(Project.id == project_id))
            cleanup.commit()
