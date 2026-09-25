from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from benchwarden.application.evaluations import EvaluationService
from benchwarden.persistence.models import CaseResult, EvaluationRun
from benchwarden.persistence.models import TestCase as Case
from benchwarden.providers.base import ModelResponse, Usage
from benchwarden.providers.fake import FINAL

pytestmark = pytest.mark.integration

EXPECTATIONS = [
    {"name": "answer", "type": "exact_output", "expected": FINAL.text},
    {"name": "normalized", "type": "normalized_output", "expected": FINAL.text.upper()},
    {"name": "contains", "type": "required_substring", "expected": "fictional"},
    {"name": "tools", "type": "tool_selection", "expected": ["get_order"]},
    {
        "name": "args",
        "type": "tool_arguments",
        "tool_name": "get_order",
        "expected": {"order_id": "ORDER-1001"},
    },
    {
        "name": "partial",
        "type": "tool_arguments",
        "tool_name": "get_order",
        "expected": {},
        "mode": "partial",
    },
    {"name": "time", "type": "max_latency", "maximum_ms": 100000},
    {"name": "cost", "type": "max_cost", "maximum_usd": "0.00005"},
]


def configure(client, records, expectations=None, scenario="valid_tool", pricing=True):
    response = client.patch(
        f"/test-cases/{records['case']['id']}",
        json={
            "input": {"scenario": scenario},
            "expectations": EXPECTATIONS if expectations is None else expectations,
        },
    )
    assert response.status_code == 200, response.text
    if pricing:
        response = client.patch(
            f"/agent-configurations/{records['agent']['id']}",
            json={"pricing": {"input_usd_per_million": "1", "output_usd_per_million": "2"}},
        )
        assert response.status_code == 200, response.text


def create(client, records, execute=True):
    response = client.post(
        "/runs",
        json={
            "project_id": records["project"]["id"],
            "agent_configuration_id": records["agent"]["id"],
            "dataset_id": records["dataset"]["id"],
        },
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    if execute:
        response = client.post(f"/runs/{run_id}/execute")
        assert response.status_code == 200, response.text
    return run_id


def detail(client, run_id):
    result = client.get(f"/runs/{run_id}/results").json()["items"][0]
    return client.get(f"/results/{result['id']}").json()


def test_all_scorers_persist_automatic_and_repeat_is_idempotent(client, records):
    configure(client, records)
    run_id = create(client, records)
    before = detail(client, run_id)
    assert before["status"] == "completed" and before["evaluation_outcome"] == "passed"
    assert len(before["scoring_results"]) == 8
    assert Decimal(before["estimated_cost_usd"]) == Decimal(".000044")
    for score in before["scoring_results"]:
        assert score["passed"] and score["value"] == 1 and score["scorer_version"] == "1"
        assert score["scorer_type"] and score["explanation"]
        assert "expected" in score and "observed" in score
    for _ in range(2):
        response = client.post(f"/runs/{run_id}/score")
        assert response.status_code == 200, response.text
        assert response.json()["pass_rate"] == 1
    assert detail(client, run_id) == before  # Includes trace, timestamps, score IDs, and status.
    metrics = client.get(f"/runs/{run_id}/metrics").json()
    assert metrics["total_tokens"] == 33 and metrics["evaluation_coverage"] == 1
    assert metrics["tool_selection_accuracy"] == metrics["tool_argument_accuracy"] == 1


def test_scoring_failures_do_not_change_execution_status(client, records):
    configure(client, records, [{"name": "answer", "type": "exact_output", "expected": "wrong"}])
    run_id = create(client, records)
    assert client.get(f"/runs/{run_id}").json()["status"] == "completed"
    result = detail(client, run_id)
    assert result["status"] == "completed" and result["evaluation_outcome"] == "failed"
    assert not result["scoring_results"][0]["passed"]


def test_mixed_execution_errors_and_empty_expectations(client, records):
    configure(client, records, EXPECTATIONS[:1], scenario="text_only")
    for name, scenario, expectations in [
        ("error", "timeout", EXPECTATIONS[:1]),
        ("no criteria", "text_only", []),
    ]:
        assert (
            client.post(
                f"/datasets/{records['dataset']['id']}/test-cases",
                json={
                    "name": name,
                    "input": {"scenario": scenario},
                    "expected_output": None,
                    "expectations": expectations,
                },
            ).status_code
            == 201
        )
    run_id = create(client, records)
    metrics = client.get(f"/runs/{run_id}/metrics").json()
    assert metrics["completed_executions"] == 2 and metrics["execution_errors"] == 1
    assert metrics["scoreable_cases"] == metrics["passed_cases"] == 1
    assert metrics["unscored_cases"] == 2 and metrics["pass_rate"] == 1
    assert metrics["end_to_end_success_rate"] == metrics["evaluation_coverage"] == 1 / 3
    assert metrics["failure_category_counts"] == {"execution": 1}
    for result in client.get(f"/runs/{run_id}/results").json()["items"]:
        if result["status"] == "error":
            assert result["evaluation_outcome"] == "not_scored" and result["scored_at"] is None
            assert client.get(f"/results/{result['id']}").json()["scoring_results"] == []


def test_snapshot_immutability_and_explicit_scoring(client, session, records):
    configure(client, records, EXPECTATIONS[:1])
    run_id = create(client, records, execute=False)
    case = session.get(Case, UUID(records["case"]["id"]))
    case.expectations = [{"name": "answer", "type": "exact_output", "expected": "changed"}]
    session.commit()  # Simulate an external writer bypassing API freeze protection.
    assert client.post(f"/runs/{run_id}/execute").status_code == 200
    before = detail(client, run_id)
    assert before["evaluation_outcome"] == "passed"
    assert before["expectations_snapshot"][0]["expected"] == FINAL.text
    assert client.patch(f"/test-cases/{case.id}", json={"expectations": []}).status_code == 409
    assert client.post(f"/runs/{run_id}/score").status_code == 200
    assert detail(client, run_id) == before


def test_explicit_scoring_recovery_and_existing_scores_protected(client, session, records):
    configure(client, records, EXPECTATIONS[:1])
    run_id = create(client, records)
    result = session.scalar(select(CaseResult).where(CaseResult.run_id == UUID(run_id)))
    original_score = result.scoring_results[0]
    session.delete(original_score)
    result.scored_at = None
    session.commit()  # Simulate interruption after execution commit but before scoring commit.
    assert client.post(f"/runs/{run_id}/score").status_code == 200
    result.scored_at = None
    session.commit()  # Scores exist without a complete marker: must not overwrite them.
    assert client.post(f"/runs/{run_id}/score").status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"expectations": [{"name": "a", "type": "invalid"}]},
        {"expectations": [EXPECTATIONS[0], EXPECTATIONS[0]]},
        {
            "expectations": [
                {"name": "a", "type": "exact_output", "expected": {"api_key": "secret"}}
            ]
        },
        {"expectations": None},
    ],
)
def test_expectation_api_validation(client, records, payload):
    assert client.patch(f"/test-cases/{records['case']['id']}", json=payload).status_code == 422


@pytest.mark.parametrize(
    "pricing",
    [
        {"currency": "EUR", "input_usd_per_million": 1, "output_usd_per_million": 1},
        {"input_usd_per_million": -1, "output_usd_per_million": 1},
        {"input_usd_per_million": "NaN", "output_usd_per_million": 1},
    ],
)
def test_pricing_validation(client, records, pricing):
    assert (
        client.patch(
            f"/agent-configurations/{records['agent']['id']}", json={"pricing": pricing}
        ).status_code
        == 422
    )


def test_cost_without_pricing_fails_explicitly(client, records):
    configure(client, records, EXPECTATIONS[-1:], pricing=False)
    run_id = create(client, records)
    result = detail(client, run_id)
    assert result["estimated_cost_usd"] is None and result["evaluation_outcome"] == "failed"
    assert "unavailable" in result["scoring_results"][0]["explanation"]


class DifferentAnswer:
    def complete(self, request):
        return ModelResponse(
            text="different", usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3)
        )


def test_comparison_metrics_classifications_and_no_writes(client, session, records):
    configure(client, records, EXPECTATIONS[:1], scenario="text_only")
    baseline = create(client, records)
    candidate = create(client, records, execute=False)
    EvaluationService(session, provider=DifferentAnswer()).execute(UUID(candidate))
    before = deepcopy(detail(client, baseline))
    comparison = client.get(f"/runs/{baseline}/compare/{candidate}")
    assert comparison.status_code == 200, comparison.text
    data = comparison.json()
    assert data["cases"][0]["classification"] == "regressed"
    assert data["cases"][0]["regressed_expectations"] == ["answer"]
    assert Decimal(data["deltas"]["pass_rate"]["absolute"]) == -1
    assert Decimal(data["deltas"]["pass_rate"]["percent"]) == -100
    assert data["failure_category_deltas"] == {"output_correctness": 1}
    reverse = client.get(f"/runs/{candidate}/compare/{baseline}").json()
    assert reverse["cases"][0]["classification"] == "improved"
    assert reverse["deltas"]["pass_rate"]["percent"] is None
    assert (
        client.get(f"/runs/{baseline}/compare/{baseline}").json()["cases"][0]["classification"]
        == "unchanged"
    )
    assert detail(client, baseline) == before


@pytest.mark.parametrize(
    "mismatch", ["dataset", "expectations", "version", "input", "score_version", "missing_scores"]
)
def test_comparison_incompatibilities(client, session, records, mismatch):
    configure(client, records, EXPECTATIONS[:1])
    baseline, candidate = create(client, records), create(client, records)
    run = session.get(EvaluationRun, UUID(candidate))
    result = session.scalar(select(CaseResult).where(CaseResult.run_id == run.id))
    if mismatch == "dataset":
        # Different run's valid empty dataset; compatibility rejects before inspecting cases.
        from benchwarden.persistence.models import EvaluationDataset

        dataset = EvaluationDataset(project_id=run.project_id, name="unrelated")
        session.add(dataset)
        session.flush()
        other = EvaluationRun(
            project_id=run.project_id,
            agent_configuration_id=run.agent_configuration_id,
            dataset_id=dataset.id,
        )
        session.add(other)
        session.flush()
        candidate = str(other.id)
    elif mismatch == "expectations":
        result.expectations_snapshot = []
    elif mismatch == "version":
        run.scoring_version = "2"
    elif mismatch == "input":
        result.input_snapshot = {"changed": True}
    elif mismatch == "score_version":
        result.scoring_results[0].scorer_version = "2"
    else:
        result.scored_at = None
    session.commit()
    assert client.get(f"/runs/{baseline}/compare/{candidate}").status_code == 409


def test_pending_legacy_missing_and_invalid_routes(client, session, records):
    run_id = create(client, records, execute=False)
    assert client.post(f"/runs/{run_id}/score").status_code == 409
    assert client.get(f"/runs/{run_id}/compare/{run_id}").status_code == 409
    assert client.get(f"/runs/{run_id}/metrics").json()["evaluation_coverage"] == 0
    client.post(f"/runs/{run_id}/execute")
    run = session.get(EvaluationRun, UUID(run_id))
    run.scoring_version = None
    session.commit()
    assert client.post(f"/runs/{run_id}/score").status_code == 409
    for value, expected in [(str(uuid4()), 404), ("invalid", 422)]:
        assert client.post(f"/runs/{value}/score").status_code == expected
        assert client.get(f"/runs/{value}/metrics").status_code == expected
        assert client.get(f"/runs/{run_id}/compare/{value}").status_code == expected


def test_unmatched_cases_reported_explicitly(client, session, records):
    from datetime import UTC, datetime

    from benchwarden.domain.status import CaseStatus

    configure(client, records, EXPECTATIONS[:1])
    baseline, candidate = create(client, records), create(client, records)
    case = Case(
        dataset_id=UUID(records["dataset"]["id"]),
        name="external extra case",
        input={},
        expected_output=None,
    )
    session.add(case)
    session.flush()
    extra = CaseResult(
        run_id=UUID(candidate),
        dataset_id=case.dataset_id,
        test_case_id=case.id,
        status=CaseStatus.COMPLETED,
        expectations_snapshot=[],
        scored_at=datetime.now(UTC),
    )
    session.add(extra)
    session.commit()
    response = client.get(f"/runs/{baseline}/compare/{candidate}")
    assert response.status_code == 200, response.text
    assert not response.json()["identical_case_set"]
    unmatched = [item for item in response.json()["cases"] if item["classification"] == "unmatched"]
    assert len(unmatched) == 1 and unmatched[0]["baseline_result_id"] is None
    assert unmatched[0]["candidate_result_id"] == str(extra.id)


def test_missing_usage_and_cleared_pricing(client, session, records):
    configure(client, records, EXPECTATIONS[-1:])
    assert (
        client.patch(
            f"/agent-configurations/{records['agent']['id']}", json={"pricing": None}
        ).status_code
        == 200
    )
    run_id = create(client, records, execute=False)

    class NoUsage:
        def complete(self, request):
            return ModelResponse(text="ok")

    EvaluationService(session, provider=NoUsage()).execute(UUID(run_id))
    metrics = client.get(f"/runs/{run_id}/metrics").json()
    assert metrics["total_tokens"] is None and metrics["estimated_cost_usd"] is None
    assert detail(client, run_id)["evaluation_outcome"] == "failed"


def test_comparison_distinguishes_boolean_and_number_snapshots(client, session, records):
    configure(client, records, [{"name": "answer", "type": "exact_output", "expected": True}])
    baseline = create(client, records)
    case = session.get(Case, UUID(records["case"]["id"]))
    case.expectations = [{"name": "answer", "type": "exact_output", "expected": 1}]
    session.commit()
    candidate = create(client, records)
    assert client.get(f"/runs/{baseline}/compare/{candidate}").status_code == 409


def test_scoring_demo_is_idempotent_and_evaluated(session):
    from benchwarden.application.evaluations import create_run
    from benchwarden.application.scoring import results_for_run
    from benchwarden.demo import seed_demo
    from benchwarden.scoring.metrics import aggregate

    ids = seed_demo(session, scoring=True)
    assert seed_demo(session, scoring=True) == ids
    run = create_run(session, *ids)
    EvaluationService(session).execute(run.id)
    metrics = aggregate(results_for_run(session, run.id))
    assert metrics.passed_cases == 3 and metrics.execution_errors == 5
    assert metrics.pass_rate == 1 and metrics.end_to_end_success_rate == 0.375
