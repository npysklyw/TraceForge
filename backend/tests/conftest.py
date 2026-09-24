"""Real PostgreSQL tests: migrate a unique schema and roll back each test."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from traceforge.main import app
from traceforge.persistence.database import get_session


def alembic_config(connection):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def database_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "Set TEST_DATABASE_URL to a PostgreSQL database with CREATE SCHEMA permission. "
            "Use pytest -m 'not integration' for unit tests only."
        )
    schema = f"traceforge_test_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -ctimezone=UTC"})
    try:
        with engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def session(database_engine):
    with database_engine.connect() as connection:
        transaction = connection.begin()
        with Session(
            connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as db:
            yield db
        transaction.rollback()


@pytest.fixture
def client(session):
    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def records(client):
    project = client.post("/projects", json={"name": "Synthetic arithmetic"}).json()
    agent = client.post(
        f"/projects/{project['id']}/agent-configurations", json={"name": "Fake v1"}
    ).json()
    dataset = client.post(f"/projects/{project['id']}/datasets", json={"name": "Addition"}).json()
    case = client.post(
        f"/datasets/{dataset['id']}/test-cases",
        json={
            "name": "Two plus three",
            "input": {"numbers": [2, 3]},
            "expected_output": 5,
        },
    ).json()
    return {"project": project, "agent": agent, "dataset": dataset, "case": case}
