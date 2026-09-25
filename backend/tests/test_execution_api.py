import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from benchwarden.application.evaluations import EvaluationService
from benchwarden.demo import seed_demo
from benchwarden.domain.status import CaseStatus, RunStatus
from benchwarden.persistence.models import (
    CaseResult,
    EvaluationRun,
    ExecutionEvent,
)
from benchwarden.persistence.models import (
    TestCase as Case,
)
from benchwarden.providers.fake import FakeModelProvider

pytestmark = pytest.mark.integration


def new_run(client, records, scenario="text_only"):
    changed = client.patch(
        f"/test-cases/{records['case']['id']}", json={"input": {"scenario": scenario}}
    )
    assert changed.status_code == 200, changed.text
    response = client.post(
        "/runs",
        json={
            "project_id": records["project"]["id"],
            "agent_configuration_id": records["agent"]["id"],
            "dataset_id": records["dataset"]["id"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def detail(client, run_id):
    results = client.get(f"/runs/{run_id}/results")
    assert results.status_code == 200
    return client.get(f"/results/{results.json()['items'][0]['id']}").json()


@pytest.mark.parametrize(
    "scenario,status,error",
    [
        ("text_only", "completed", None),
        ("valid_tool", "completed", None),
        ("unknown_tool", "failed", "unknown_tool"),
        ("invalid_arguments", "failed", "invalid_tool_arguments"),
        ("timeout", "failed", "provider_timeout"),
        ("provider_error", "failed", "provider_error"),
        ("tool_failure", "failed", "tool_error"),
        ("refund_flow", "completed", None),
    ],
)
def test_execution_api_scenarios(client, records, scenario, status, error):
    run = new_run(client, records, scenario)
    assert run["status"] == "pending"
    assert run["started_at"] is None
    assert run["case_counts"]["pending"] == 1
    assert run["total_cases"] == 1
    pending = detail(client, run["id"])
    assert pending["events"] == []
    assert pending["status"] == "pending"
    response = client.post(f"/runs/{run['id']}/execute")
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["status"] == status
    assert completed["started_at"] and completed["finished_at"]
    result = detail(client, run["id"])
    assert result["status"] == ("error" if error else "completed")
    assert result["error_type"] == error
    assert result["input_snapshot"] == {"scenario": scenario}
    assert result["provider"] == "fake"
    assert result["model_name"] == "deterministic-v1"
    assert result["response"] is None if error else bool(result["response"])
    assert client.get(f"/runs/{run['id']}").json() == completed
    assert client.post(f"/runs/{run['id']}/execute").status_code == 409
    assert detail(client, run["id"]) == result  # No duplicate events or tool side effects.


def test_timing_tokens_and_order_persist(client, records):
    run = new_run(client, records, "valid_tool")
    client.post(f"/runs/{run['id']}/execute")
    result = detail(client, run["id"])
    assert (result["input_tokens"], result["output_tokens"], result["total_tokens"]) == (22, 11, 33)
    events = result["events"]
    assert [item["sequence"] for item in events] == list(range(8))
    assert [item["kind"] for item in events] == [
        "case_started",
        "model_request",
        "model_response",
        "tool_request",
        "tool_result",
        "model_request",
        "model_response",
        "case_completed",
    ]
    assert events[2]["total_tokens"] == 15
    assert events[6]["total_tokens"] == 18
    tool = result["tool_calls"][0]
    assert tool["arguments"] == {"order_id": "ORDER-1001"}
    assert tool["arguments_validated"]
    assert tool["output"]["amount_cents"] == 4200
    assert tool["provider_call_id"] == "fake-call-1"
    for record in [result, tool, *events]:
        start = datetime.fromisoformat(record["started_at"])
        end = datetime.fromisoformat(record["finished_at"])
        assert start.utcoffset() == end.utcoffset() == timedelta(0)
        assert start <= end
        assert record["latency_ms"] >= 0
    assert result["latency_ms"] >= tool["latency_ms"]


def test_mixed_cases_continue_after_failure_with_commits(client, session, records):
    client.patch(f"/test-cases/{records['case']['id']}", json={"input": {"scenario": "timeout"}})
    # Explicit ordering makes the failed case run before the successful case.
    first = session.get(Case, UUID(records["case"]["id"]))
    first.created_at = datetime(2020, 1, 1, tzinfo=datetime.now().astimezone().tzinfo)
    session.commit()
    second = client.post(
        f"/datasets/{records['dataset']['id']}/test-cases",
        json={
            "name": "second",
            "input": {"scenario": "valid_tool"},
            "expected_output": None,
        },
    )
    assert second.status_code == 201
    run = client.post(
        "/runs",
        json={
            "project_id": records["project"]["id"],
            "agent_configuration_id": records["agent"]["id"],
            "dataset_id": records["dataset"]["id"],
        },
    ).json()
    observed = []

    class TransactionProbe(FakeModelProvider):
        def complete(self, request):
            assert not session.in_transaction(), "Provider call must not hold a transaction"
            observed.append(request.input["scenario"])
            return super().complete(request)

    EvaluationService(session, provider=TransactionProbe()).execute(UUID(run["id"]))
    results = client.get(f"/runs/{run['id']}/results").json()
    assert sorted(result["status"] for result in results["items"]) == ["completed", "error"]
    summary = client.get(f"/runs/{run['id']}").json()
    assert summary["case_counts"]["error"] == summary["case_counts"]["completed"] == 1
    assert summary["status"] == "failed"
    assert observed == ["timeout", "valid_tool", "valid_tool"]


@pytest.mark.parametrize(
    "status", [RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED]
)
def test_only_pending_run_can_execute(client, session, records, status):
    run = new_run(client, records)
    persisted = session.get(EvaluationRun, UUID(run["id"]))
    persisted.status = status
    session.commit()
    assert client.post(f"/runs/{run['id']}/execute").status_code == 409
    assert session.scalar(select(func.count()).select_from(ExecutionEvent)) == 0


def test_started_case_cannot_execute_again(client, session, records):
    run = new_run(client, records)
    result = session.scalar(select(CaseResult).where(CaseResult.run_id == UUID(run["id"])))
    result.status = CaseStatus.RUNNING
    session.commit()
    assert client.post(f"/runs/{run['id']}/execute").status_code == 409


@pytest.mark.parametrize(
    "path,method",
    [
        ("/runs/{id}", "get"),
        ("/runs/{id}/execute", "post"),
        ("/runs/{id}/results", "get"),
        ("/results/{id}", "get"),
    ],
)
def test_missing_run_or_result(client, path, method):
    assert getattr(client, method)(path.format(id=uuid4())).status_code == 404
    assert getattr(client, method)(path.format(id="invalid")).status_code == 422


def test_create_validation_missing_resources_and_mismatched_project(client, records):
    payload = {
        "project_id": records["project"]["id"],
        "agent_configuration_id": records["agent"]["id"],
        "dataset_id": records["dataset"]["id"],
    }
    assert client.post("/runs", json={}).status_code == 422
    assert client.post("/runs", json={**payload, "extra": True}).status_code == 422
    for field in payload:
        assert client.post("/runs", json={**payload, field: str(uuid4())}).status_code == 404
    other = client.post("/projects", json={"name": "other"}).json()
    assert client.post("/runs", json={**payload, "project_id": other["id"]}).status_code == 422
    client.patch(f"/agent-configurations/{records['agent']['id']}", json={"provider": "openai"})
    assert client.post("/runs", json=payload).status_code == 422
    client.patch(
        f"/agent-configurations/{records['agent']['id']}",
        json={"provider": "fake", "parameters": {"max_steps": 0}},
    )
    assert client.post("/runs", json=payload).status_code == 422


def test_empty_dataset_and_result_pagination(client, records):
    run = new_run(client, records)
    assert client.get(f"/runs/{run['id']}/results?offset=1").json()["items"] == []
    assert client.get(f"/runs/{run['id']}/results?limit=101").status_code == 422
    dataset = client.post(
        f"/projects/{records['project']['id']}/datasets", json={"name": "empty"}
    ).json()
    response = client.post(
        "/runs",
        json={
            "project_id": records["project"]["id"],
            "agent_configuration_id": records["agent"]["id"],
            "dataset_id": dataset["id"],
        },
    )
    assert response.status_code == 422
    assert "at least one" in response.text


def test_persisted_trace_excludes_secrets_and_internal_reasoning(client, session, records):
    # Simulate legacy unsafe source data; execution must not copy it into traces.
    case = session.get(Case, UUID(records["case"]["id"]))
    case.input = {
        "scenario": "valid_tool",
        "api_key": "SECRET-SOURCE",
        "nested": {"chain_of_thought": "PRIVATE-REASONING"},
    }
    session.commit()
    run = client.post(
        "/runs",
        json={
            "project_id": records["project"]["id"],
            "agent_configuration_id": records["agent"]["id"],
            "dataset_id": records["dataset"]["id"],
        },
    ).json()
    client.post(f"/runs/{run['id']}/execute")
    result = detail(client, run["id"])
    serialized = json.dumps(result)
    for word in ("SECRET-SOURCE", "PRIVATE-REASONING", "api_key", "chain_of_thought"):
        assert word not in serialized
    persisted = session.scalar(select(CaseResult).where(CaseResult.id == UUID(result["id"])))
    assert persisted.input_snapshot == {"scenario": "valid_tool", "nested": {}}


def test_seed_demo_reuses_scenarios_and_is_idempotent(session):
    ids = seed_demo(session)
    assert seed_demo(session) == ids
    cases = list(session.scalars(select(Case).where(Case.dataset_id == ids[2])))
    assert len(cases) == 8
    assert {case.input["scenario"] for case in cases} >= {"text_only", "refund_flow", "timeout"}


@pytest.mark.parametrize(
    "payload",
    [
        {"input": {"api_key": "SECRET-MUST-NOT-BE-SAVED"}},
        {"input": {"message": "Bearer SECRET-MUST-NOT-BE-SAVED"}},
        {"expected_output": {"internal_reasoning": "PRIVATE-THOUGHTS"}},
    ],
)
def test_sensitive_source_data_is_rejected_before_persistence(client, records, payload):
    path = f"/test-cases/{records['case']['id']}"
    before = client.get(path).json()
    response = client.patch(path, json=payload)
    assert response.status_code == 422
    assert "before saving" in response.text
    assert client.get(path).json() == before
