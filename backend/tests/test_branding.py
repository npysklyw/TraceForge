"""Configuration upgrades and seed compatibility across the product rename."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from benchwarden.application.evaluations import EvaluationService, create_run
from benchwarden.demo import seed_demo
from benchwarden.main import app
from benchwarden.persistence.models import Project, ScoringResult
from benchwarden.settings import Settings


def test_application_branding():
    with TestClient(app) as client:
        assert client.get("/openapi.json").json()["info"]["title"] == "Benchwarden"
        assert client.get("/health").json() == {"status": "ok"}


def test_database_configuration_prefix_and_legacy_connection(monkeypatch):
    monkeypatch.delenv("BENCHWARDEN_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url.get_secret_value().endswith("/benchwarden")
    old_url = "postgresql+psycopg://existing:example@localhost/existing"
    new_url = "postgresql+psycopg://new:example@localhost/new"
    monkeypatch.setenv("DATABASE_URL", old_url)
    assert Settings(_env_file=None).database_url.get_secret_value() == old_url
    monkeypatch.setenv("BENCHWARDEN_DATABASE_URL", new_url)
    settings = Settings(_env_file=None)
    assert settings.database_url.get_secret_value() == new_url
    assert new_url not in repr(settings)


@pytest.mark.integration
@pytest.mark.parametrize("scoring", [False, True])
def test_existing_demo_is_reused_without_changing_stored_data(session, scoring):
    ids = seed_demo(session, scoring=scoring)
    project = session.get(Project, ids[0])
    # Emulate a populated installation created before the rename.
    old_name = (
        "TraceForge fictional support scoring demo"
        if scoring
        else "TraceForge fictional support demo"
    )
    project.name = old_name
    session.commit()
    run = create_run(session, *ids)
    EvaluationService(session).execute(run.id)
    before = [(r.id, r.response, r.status, r.scored_at) for r in run.case_results]
    scores = session.scalar(select(func.count()).select_from(ScoringResult))
    assert seed_demo(session, scoring=scoring) == ids
    session.expire_all()
    assert project.name == old_name
    assert [(r.id, r.response, r.status, r.scored_at) for r in run.case_results] == before
    assert session.scalar(select(func.count()).select_from(ScoringResult)) == scores
    assert session.scalar(select(func.count()).select_from(Project)) == 1
