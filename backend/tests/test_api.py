from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from benchwarden.persistence.models import EvaluationRun

pytestmark = pytest.mark.integration


def test_crud_lifecycle_and_relationships(client, records):
    project, agent, dataset, case = (
        records[key] for key in ("project", "agent", "dataset", "case")
    )
    assert agent["project_id"] == project["id"]
    assert dataset["project_id"] == project["id"]
    assert case["dataset_id"] == dataset["id"]
    for resource, record in [
        ("projects", project),
        ("agent-configurations", agent),
        ("datasets", dataset),
        ("test-cases", case),
    ]:
        UUID(record["id"])
        assert datetime.fromisoformat(record["created_at"]).utcoffset() == timedelta(0)
        assert client.get(f"/{resource}/{record['id']}").json() == record
        response = client.patch(f"/{resource}/{record['id']}", json={"name": "renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "renamed"
        assert response.json()["created_at"] == record["created_at"]
        assert response.json()["updated_at"] >= record["updated_at"]
    for path in [
        f"/projects/{project['id']}/agent-configurations",
        f"/projects/{project['id']}/datasets",
        f"/datasets/{dataset['id']}/test-cases",
    ]:
        response = client.get(path).json()
        assert response["total"] == 1
        assert len(response["items"]) == 1
    assert client.delete(f"/projects/{project['id']}").status_code == 409
    assert client.delete(f"/datasets/{dataset['id']}").status_code == 409
    for resource, record in [
        ("test-cases", case),
        ("datasets", dataset),
        ("agent-configurations", agent),
        ("projects", project),
    ]:
        response = client.delete(f"/{resource}/{record['id']}")
        assert response.status_code == 204
        assert not response.content
        assert client.get(f"/{resource}/{record['id']}").status_code == 404


def test_pagination_and_scope(client, records):
    other = client.post("/projects", json={"name": "Other"}).json()
    first = client.get("/projects?limit=1").json()
    second = client.get("/projects?limit=1&offset=1").json()
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert client.get("/projects?offset=99").json()["items"] == []
    for child in ("datasets", "agent-configurations"):
        assert client.get(f"/projects/{other['id']}/{child}").json()["total"] == 0
    for query in ("limit=0", "limit=101", "offset=-1", "limit=abc", "offset=9223372036854775808"):
        assert client.get(f"/projects?{query}").status_code == 422


@pytest.mark.parametrize("resource", ["projects", "agent-configurations", "datasets", "test-cases"])
def test_missing_and_malformed_resources(client, resource):
    missing = f"/{resource}/{uuid4()}"
    assert client.get(missing).status_code == 404
    assert client.patch(missing, json={"name": "new"}).status_code == 404
    assert client.delete(missing).status_code == 404
    assert client.get(f"/{resource}/not-a-uuid").status_code == 422


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/projects/{id}/agent-configurations", {"name": "agent"}),
        ("/projects/{id}/datasets", {"name": "dataset"}),
        ("/datasets/{id}/test-cases", {"name": "case", "input": {}, "expected_output": None}),
    ],
)
def test_missing_parents(client, path, payload):
    path = path.format(id=uuid4())
    assert client.post(path, json=payload).status_code == 404
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "payload", [{}, {"name": " "}, {"name": "a" * 201}, {"name": "valid", "surprise": True}]
)
def test_create_validation_errors_have_field_locations(client, payload):
    response = client.post("/projects", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "body"


def test_agent_case_and_dataset_validation(client, records):
    project_id = records["project"]["id"]
    dataset_id = records["dataset"]["id"]
    assert (
        client.post(
            f"/projects/{project_id}/agent-configurations",
            json={
                "name": "invalid",
                "provider": "unsupported",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/projects/{project_id}/datasets",
            json={
                "name": "invalid",
                "schema_version": 2,
            },
        ).status_code
        == 422
    )
    for payload in (
        {"name": "missing", "input": {}},
        {"name": "wrong input", "input": [], "expected_output": 1},
    ):
        assert client.post(f"/datasets/{dataset_id}/test-cases", json=payload).status_code == 422
    for resource, key in [
        ("projects", "project"),
        ("agent-configurations", "agent"),
        ("datasets", "dataset"),
        ("test-cases", "case"),
    ]:
        for payload in ({}, {"name": None}, {"project_id": str(uuid4())}):
            assert (
                client.patch(f"/{resource}/{records[key]['id']}", json=payload).status_code == 422
            )


def test_json_roundtrip_and_patch_semantics(client, records):
    path = f"/test-cases/{records['case']['id']}"
    expected = {"nested": [1, True, None, {"text": "hello"}]}
    assert (
        client.patch(path, json={"expected_output": expected}).json()["expected_output"] == expected
    )
    assert client.patch(path, json={"name": "rename"}).json()["expected_output"] == expected
    assert client.patch(path, json={"expected_output": None}).json()["expected_output"] is None
    assert client.patch(path, json={"input": None}).status_code == 422
    response = client.patch(path, json={"input": {"text": "x" * 262144}})
    assert response.status_code == 422
    assert "256 KiB" in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "bad\u0000name"},
        {"input": {"text": "bad\u0000value"}},
        {"expected_output": {"bad\u0000key": "value"}},
    ],
)
def test_postgres_unsupported_text_returns_validation_error(client, records, payload):
    response = client.patch(f"/test-cases/{records['case']['id']}", json=payload)
    assert response.status_code == 422
    assert "Unicode null" in response.text


def test_duplicate_conflicts_and_session_recovers(client, records):
    project = records["project"]
    assert client.post("/projects", json={"name": project["name"]}).status_code == 409
    for path, payload in [
        (f"/projects/{project['id']}/agent-configurations", {"name": records["agent"]["name"]}),
        (f"/projects/{project['id']}/datasets", {"name": records["dataset"]["name"]}),
        (
            f"/datasets/{records['dataset']['id']}/test-cases",
            {"name": records["case"]["name"], "input": {}, "expected_output": 0},
        ),
    ]:
        assert client.post(path, json=payload).status_code == 409
    other = client.post("/projects", json={"name": "Other"})
    assert other.status_code == 201
    assert (
        client.patch(f"/projects/{other.json()['id']}", json={"name": project["name"]}).status_code
        == 409
    )
    assert client.get(f"/projects/{other.json()['id']}").json()["name"] == "Other"
    assert (
        client.post(
            f"/projects/{other.json()['id']}/datasets", json={"name": records["dataset"]["name"]}
        ).status_code
        == 201
    )


def test_records_used_by_runs_are_frozen(client, session, records):
    run = EvaluationRun(
        project_id=UUID(records["project"]["id"]),
        agent_configuration_id=UUID(records["agent"]["id"]),
        dataset_id=UUID(records["dataset"]["id"]),
    )
    session.add(run)
    session.commit()
    for resource, key in [
        ("agent-configurations", "agent"),
        ("datasets", "dataset"),
        ("test-cases", "case"),
    ]:
        path = f"/{resource}/{records[key]['id']}"
        assert client.patch(path, json={"name": "changed"}).status_code == 409
        assert client.delete(path).status_code == 409
        assert client.get(path).status_code == 200
    assert (
        client.post(
            f"/datasets/{records['dataset']['id']}/test-cases",
            json={
                "name": "new",
                "input": {},
                "expected_output": None,
            },
        ).status_code
        == 409
    )
