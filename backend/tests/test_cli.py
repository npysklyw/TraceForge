import json
import socket
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from benchwarden import cli
from benchwarden.application.evaluations import create_run
from benchwarden.domain.status import RunStatus
from benchwarden.persistence.models import AgentConfiguration, CaseResult, EvaluationRun
from benchwarden.persistence.models import TestCase as Case
from benchwarden.settings import Settings


@pytest.fixture
def invoke(session, monkeypatch, capsys):
    monkeypatch.setattr(cli, "session_scope", lambda: nullcontext(session))

    def call(*args, code=0):
        assert cli.main([*map(str, args), "--json"]) == code
        captured = capsys.readouterr()
        assert captured.err == ""
        assert len(captured.out.splitlines()) == 1
        report = json.loads(captured.out)
        assert set(report) == {"schema_version", "command", "ok", "exit_code", "data", "error"}
        assert report["schema_version"] == 1
        assert report["exit_code"] == code
        assert report["ok"] is (code == 0)
        return report

    return call


def run_pair(invoke, regression=False):
    seed = invoke("seed-ci", *(["--regression"] if regression else []))["data"]
    runs = [
        invoke("run", seed["dataset_id"], "--config", seed[key])["data"]["run_id"]
        for key in ("baseline_config_id", "candidate_config_id")
    ]
    return seed, runs


@pytest.mark.integration
@pytest.mark.parametrize("regression", [False, True])
def test_offline_workflow(invoke, session, monkeypatch, regression):
    def no_network(*args, **kwargs):
        raise AssertionError("Model execution must not open network sockets")

    # psycopg uses libpq; Python network calls by a model/provider are prohibited.
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    seed, (baseline, candidate) = run_pair(invoke, regression)
    before = session.scalars(select(CaseResult).where(CaseResult.run_id == UUID(candidate))).all()
    timestamps = [r.scored_at for r in before]
    for run_id in (baseline, candidate):
        scored = invoke("score", run_id)["data"]
        assert scored["execution_status"] == "completed"
        assert scored["metrics"]["total_cases"] == 2
        assert scored["metrics"]["total_tokens"] == 51
        assert isinstance(scored["metrics"]["estimated_cost_usd"], str)
    assert timestamps == [r.scored_at for r in before]
    pair = invoke("compare", baseline, candidate)["data"]
    assert {c["classification"] for c in pair["cases"]} == {
        "regressed" if regression else "unchanged"
    }
    report = invoke(
        "check",
        candidate,
        "--baseline",
        baseline,
        "--min-pass-rate",
        "1",
        "--min-end-to-end-success-rate",
        "1",
        "--min-evaluation-coverage",
        "1",
        "--max-execution-errors",
        "0",
        "--max-token-increase",
        "0",
        "--max-cost-increase",
        "0",
        "--max-regressions",
        "0",
        code=int(regression),
    )["data"]
    assert report["passed"] is (not regression)
    assert len(report["checks"]) == 7
    assert {c["name"] for c in report["checks"] if not c["passed"]} == (
        {"min_pass_rate", "min_end_to_end_success_rate", "max_regressions"} if regression else set()
    )


@pytest.mark.integration
def test_execution_errors_are_distinct_from_quality(invoke, session):
    seed = invoke("seed-ci")["data"]
    agent = session.get(AgentConfiguration, UUID(seed["candidate_config_id"]))
    agent.parameters = {"max_steps": 1}
    session.commit()
    run = invoke("run", seed["dataset_id"], "--config", agent.id, code=3)["data"]
    assert run["execution_status"] == "failed"
    assert run["metrics"]["execution_errors"] == 1
    invoke("score", run["run_id"])
    invoke("check", run["run_id"], "--max-execution-errors", "0", code=1)


@pytest.mark.integration
def test_incompatible_runs_missing_metrics_and_pricing(invoke, session):
    _, (baseline, candidate) = run_pair(invoke)
    _, (_, unrelated) = run_pair(invoke)
    invoke("compare", baseline, unrelated, code=2)
    invoke("check", unrelated, "--baseline", baseline, "--max-regressions", "0", code=2)
    result = session.scalars(select(CaseResult).where(CaseResult.run_id == UUID(candidate))).first()
    result.estimated_cost_usd = None
    session.commit()
    check = invoke("check", candidate, "--baseline", baseline, "--max-cost-increase", "0", code=1)[
        "data"
    ]["checks"][0]
    assert check["reason"] == "metric_unavailable"
    run = session.get(EvaluationRun, UUID(candidate))
    run.pricing_snapshot = {"input_usd_per_million": "2", "output_usd_per_million": "2"}
    session.commit()
    invoke("check", candidate, "--baseline", baseline, "--max-cost-increase", "0", code=2)


@pytest.mark.integration
def test_missing_ids_and_invalid_provider_configuration(invoke, session):
    for command in ("score", "check"):
        extra = ["--min-pass-rate", "0"] if command == "check" else []
        invoke(command, uuid4(), *extra, code=2)
    invoke("compare", uuid4(), uuid4(), code=2)
    invoke("run", uuid4(), "--config", uuid4(), code=2)
    seed = invoke("seed-ci")["data"]
    agent = session.get(AgentConfiguration, UUID(seed["baseline_config_id"]))
    agent.parameters = {"fake_variant": "secret-invalid-variant"}
    session.commit()
    report = invoke("run", seed["dataset_id"], "--config", agent.id, code=2)
    assert "secret-invalid-variant" not in json.dumps(report)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["score", "postgresql://user:secret-password@host/db"],
        ["run", str(uuid4())],
        ["compare", "bad", "bad"],
        ["check", str(uuid4())],
        ["check", str(uuid4()), "--min-pass-rate", "NaN"],
        ["check", str(uuid4()), "--min-pass-rate", "90"],
        ["check", str(uuid4()), "--max-cost-increase", "0.2"],
        ["check", str(uuid4()), "--max-regressions", "0.5"],
    ],
)
def test_invalid_input_does_not_connect_or_echo_secrets(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "session_scope", lambda: pytest.fail("must validate first"))
    assert cli.main(["--json", *args]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    report = json.loads(output.out)
    assert report["error"]["type"] == "invalid_input"
    assert "secret-password" not in output.out


def test_operational_failure_redacted(monkeypatch, capsys):
    def unavailable():
        raise OperationalError("postgresql://user:secret-password@host/db", {}, Exception("token"))

    monkeypatch.setattr(cli, "session_scope", unavailable)
    assert cli.main(["seed-ci", "--json"]) == 3
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["error"]["type"] == "operational_failure"
    assert "secret-password" not in output.out
    assert "postgresql" not in output.out
    assert output.err == ""


@pytest.mark.parametrize("command", ["", "run", "score", "compare", "check", "seed-ci"])
def test_help(command, capsys):
    assert cli.main([*([command] if command else []), "--help"]) == 0
    assert "benchwarden" in capsys.readouterr().out


@pytest.mark.integration
def test_human_output(session, monkeypatch, capsys):
    monkeypatch.setattr(cli, "session_scope", lambda: nullcontext(session))
    assert cli.main(["seed-ci"]) == 0
    assert "Benchwarden seed-ci: PASS" in capsys.readouterr().out
    assert cli.main(["score", "bad"]) == 2
    assert "Invalid arguments" in capsys.readouterr().err


@pytest.mark.integration
def test_gate_rejects_unfinished_unscored_and_empty_runs(invoke, session):
    seed = invoke("seed-ci")["data"]
    pending = create_run(
        session,
        UUID(seed["project_id"]),
        UUID(seed["baseline_config_id"]),
        UUID(seed["dataset_id"]),
    )
    invoke("check", pending.id, "--min-pass-rate", "0", code=2)
    _, (_, candidate) = run_pair(invoke)
    result = session.scalars(select(CaseResult).where(CaseResult.run_id == UUID(candidate))).first()
    result.scored_at = None
    session.commit()
    invoke("check", candidate, "--min-pass-rate", "0", code=2)
    for result in list(pending.case_results):
        session.delete(result)
    pending.status = RunStatus.COMPLETED
    pending.finished_at = datetime.now(UTC)
    session.commit()
    invoke("check", pending.id, "--min-pass-rate", "0", code=2)


@pytest.mark.integration
def test_gate_rejects_unmatched_cases_and_changed_snapshots(invoke, session):
    seed, (baseline, candidate) = run_pair(invoke)
    result = session.scalars(select(CaseResult).where(CaseResult.run_id == UUID(candidate))).first()
    original = result.expectations_snapshot
    result.expectations_snapshot = [{**original[0], "expected": "different"}, original[1]]
    session.commit()
    invoke("check", candidate, "--baseline", baseline, "--max-regressions", "0", code=2)
    result.expectations_snapshot = original
    session.add(
        Case(
            dataset_id=UUID(seed["dataset_id"]),
            name="added later",
            input={"scenario": "text_only"},
            expected_output=None,
            expectations=[],
        )
    )
    session.commit()
    candidate = invoke("run", seed["dataset_id"], "--config", seed["candidate_config_id"])["data"][
        "run_id"
    ]
    pair = invoke("compare", baseline, candidate)["data"]
    assert not pair["identical_case_set"]
    invoke("check", candidate, "--baseline", baseline, "--max-regressions", "0", code=2)


@pytest.mark.integration
def test_report_redacts_connection_secret(invoke, session, monkeypatch):
    _, (baseline, candidate) = run_pair(invoke, regression=True)
    secret = "connection-secret-test"
    for run_id in (baseline, candidate):
        for result in session.scalars(select(CaseResult).where(CaseResult.run_id == UUID(run_id))):
            result.expectations_snapshot = [
                {**e, "name": secret if e["name"] == "answer" else e["name"]}
                for e in result.expectations_snapshot
            ]
            for score in result.scoring_results:
                if score.scorer_name == "answer":
                    score.scorer_name = secret
    session.commit()
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(database_url=f"postgresql+psycopg://user:{secret}@localhost/db"),
    )
    report = invoke("compare", baseline, candidate)
    assert secret not in json.dumps(report)
    assert "[REDACTED]" in json.dumps(report)
