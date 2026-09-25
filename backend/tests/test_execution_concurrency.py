from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from benchwarden.application.evaluations import EvaluationService, ExecutionConflict, create_run
from benchwarden.domain.status import CaseStatus, RunStatus
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
from benchwarden.persistence.models import (
    TestCase as Case,
)
from benchwarden.providers.fake import FakeModelProvider

pytestmark = pytest.mark.integration


def test_concurrent_claim_and_independent_commits(database_engine):
    entered, release = Event(), Event()
    with Session(database_engine, expire_on_commit=False) as seed:
        project = Project(name=f"Concurrency {uuid4()}")
        seed.add(project)
        seed.flush()
        agent = AgentConfiguration(
            project_id=project.id, name="fake", model_name="deterministic-v1"
        )
        dataset = EvaluationDataset(project_id=project.id, name="concurrency")
        seed.add_all([agent, dataset])
        seed.flush()
        first = Case(
            dataset_id=dataset.id, name="first", input={"scenario": "timeout"}, expected_output=None
        )
        seed.add(first)
        seed.commit()
        second = Case(
            dataset_id=dataset.id,
            name="second",
            input={"scenario": "text_only"},
            expected_output=None,
        )
        seed.add(second)
        seed.commit()
        run = create_run(seed, project.id, agent.id, dataset.id)
        run_id, project_id, dataset_id, agent_id = run.id, project.id, dataset.id, agent.id

    class BlockingProvider(FakeModelProvider):
        def complete(self, request):
            if request.input["scenario"] == "text_only":
                entered.set()
                assert release.wait(10), "Test failed to release provider"
            return super().complete(request)

    def execute():
        with Session(database_engine, expire_on_commit=False) as session:
            return EvaluationService(session, provider=BlockingProvider()).execute(run_id).status

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(execute)
            try:
                assert entered.wait(10)
                # Other connections see the first case and trace before run completion.
                with Session(database_engine, expire_on_commit=False) as other:
                    current = other.get(EvaluationRun, run_id)
                    assert current.status == RunStatus.RUNNING
                    result = other.scalar(
                        select(CaseResult).where(CaseResult.test_case_id == first.id)
                    )
                    assert result.status == CaseStatus.ERROR
                    assert result.error_type == "provider_timeout"
                    assert result.events[-1].kind == "case_completed"
                    with pytest.raises(ExecutionConflict):
                        EvaluationService(other).execute(run_id)
                    other.rollback()
            finally:
                release.set()
            assert future.result(timeout=10) == RunStatus.FAILED
        with Session(database_engine) as verify:
            results = list(verify.scalars(select(CaseResult).where(CaseResult.run_id == run_id)))
            assert len(results) == 2
            assert {result.status for result in results} == {CaseStatus.COMPLETED, CaseStatus.ERROR}
    finally:
        release.set()
        # Delete only this test's committed graph; other tests use rollback isolation.
        with Session(database_engine) as cleanup:
            result_ids = select(CaseResult.id).where(CaseResult.run_id == run_id)
            for model in (ExecutionEvent, ToolCall, ScoringResult):
                cleanup.execute(delete(model).where(model.case_result_id.in_(result_ids)))
            cleanup.execute(delete(CaseResult).where(CaseResult.run_id == run_id))
            cleanup.execute(delete(EvaluationRun).where(EvaluationRun.id == run_id))
            cleanup.execute(delete(Case).where(Case.dataset_id == dataset_id))
            cleanup.execute(delete(EvaluationDataset).where(EvaluationDataset.id == dataset_id))
            cleanup.execute(delete(AgentConfiguration).where(AgentConfiguration.id == agent_id))
            cleanup.execute(delete(Project).where(Project.id == project_id))
            cleanup.commit()
