"""Benchwarden terminal adapter. JSON stdout is a single versioned envelope."""

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Never, cast
from uuid import UUID

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from benchwarden.application.ci_seed import seed_ci
from benchwarden.application.comparison import compare_runs
from benchwarden.application.evaluations import (
    EvaluationService,
    ExecutionConflict,
    InvalidEvaluation,
    ResourceNotFound,
    create_run,
    require,
)
from benchwarden.application.gates import GatePolicy, check_run
from benchwarden.application.redaction import TraceSanitizer
from benchwarden.application.scoring import ScoringConflict, results_for_run, score_run
from benchwarden.domain.status import RunStatus
from benchwarden.persistence.database import get_engine
from benchwarden.persistence.models import AgentConfiguration, EvaluationDataset, EvaluationRun
from benchwarden.scoring.metrics import RunMetrics, aggregate
from benchwarden.settings import get_settings


class InputError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse messages can echo credential-bearing user input; never print them.
        raise InputError("Invalid arguments; use benchwarden COMMAND --help for syntax")


class RunReport(BaseModel):
    run_id: UUID
    execution_status: RunStatus
    metrics: RunMetrics


def parser() -> Parser:
    root = Parser(
        prog="benchwarden",
        description="Offline evaluation and PR regression gates",
        allow_abbrev=False,
    )
    root.add_argument("--json", action="store_true", help="Emit one JSON envelope (any position)")
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    run = commands.add_parser(
        "run", help="Execute an existing dataset using a saved configuration", allow_abbrev=False
    )
    run.add_argument("dataset", type=UUID, help="Dataset UUID")
    run.add_argument("--config", required=True, type=UUID, help="Agent configuration UUID")
    score = commands.add_parser(
        "score", help="Idempotently score a finished run", allow_abbrev=False
    )
    score.add_argument("run_id", type=UUID)
    compare = commands.add_parser(
        "compare", help="Compare compatible finished runs", allow_abbrev=False
    )
    compare.add_argument("baseline", type=UUID)
    compare.add_argument("candidate", type=UUID)
    check = commands.add_parser(
        "check", help="Check a finished run against explicit thresholds", allow_abbrev=False
    )
    check.add_argument("run_id", type=UUID)
    check.add_argument("--baseline", type=UUID, help="Required for increase/regression thresholds")
    for field in GatePolicy.model_fields:
        check.add_argument(
            "--" + field.replace("_", "-"),
            type=int if field in {"max_execution_errors", "max_regressions"} else str,
            help="See docs/cli-ci.md for units and inclusive boundary semantics",
        )
    seed = commands.add_parser(
        "seed-ci",
        help="Create an isolated two-case, two-config offline fixture",
        allow_abbrev=False,
    )
    seed.add_argument(
        "--regression", action="store_true", help="Candidate intentionally answers incorrectly"
    )
    return root


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def dispatch(
    args: argparse.Namespace, session: Session, policy: GatePolicy | None
) -> tuple[BaseModel, int]:
    if args.command == "seed-ci":
        return seed_ci(session, regression=args.regression), 0
    if args.command == "run":
        dataset = require(session, EvaluationDataset, args.dataset)
        agent = require(session, AgentConfiguration, args.config)
        run = create_run(session, dataset.project_id, agent.id, dataset.id)
        EvaluationService(session).execute(run.id)
        report = RunReport(
            run_id=run.id,
            execution_status=run.status,
            metrics=aggregate(results_for_run(session, run.id)),
        )
        return report, 3 if run.status == RunStatus.FAILED else 0
    if args.command == "compare":
        return compare_runs(
            session,
            require(session, EvaluationRun, args.baseline),
            require(session, EvaluationRun, args.candidate),
        ), 0
    run = require(session, EvaluationRun, args.run_id)
    if args.command == "score":
        score_run(session, run)
        return RunReport(
            run_id=run.id,
            execution_status=run.status,
            metrics=aggregate(results_for_run(session, run.id)),
        ), 0
    assert policy is not None
    baseline = require(session, EvaluationRun, args.baseline) if args.baseline else None
    gate = check_run(session, run, policy, baseline)
    return gate, 0 if gate.passed else 1


def render(value: JsonValue, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict | list):
                lines.append(prefix + key.replace("_", " ") + ":")
                lines.extend(render(item, indent + 2))
            else:
                lines.append(
                    prefix
                    + key.replace("_", " ")
                    + ": "
                    + ("unavailable" if item is None else str(item))
                )
        return lines
    if isinstance(value, list):
        return [line for item in value for line in render(item, indent + 2)]
    return [prefix + str(value)]


def emit(
    command: str | None,
    data: JsonValue,
    code: int,
    json_output: bool,
    error: str | None = None,
    kind: str | None = None,
) -> None:
    envelope: dict[str, JsonValue] = {
        "schema_version": 1,
        "command": command,
        "ok": code == 0,
        "exit_code": code,
        "data": data,
        "error": {"type": kind, "message": error} if error else None,
    }
    if json_output:
        print(json.dumps(envelope, ensure_ascii=True, allow_nan=False))
    elif error:
        print(f"Benchwarden: {error}", file=sys.stderr)
    else:
        label = "PASS" if code == 0 else ("FAIL" if code == 1 else "EXECUTION ERROR")
        print(f"Benchwarden {command}: {label}")
        print("\n".join(render(data)))


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in values
    command: str | None = None
    try:
        args = parser().parse_args([value for value in values if value != "--json"])
        command = args.command
        policy = (
            GatePolicy.model_validate(
                {name: getattr(args, name) for name in GatePolicy.model_fields}
            )
            if command == "check"
            else None
        )
        if policy and policy.needs_baseline and args.baseline is None:
            raise InputError("Increase thresholds and regression counts require --baseline")
        with session_scope() as session:
            report, code = dispatch(args, session, policy)
        # Project data is not dumped; reports contain IDs, metrics, and public scorer names.
        # Remove known connection credentials even if a stored scorer name happens to match one.
        connection = get_settings().database_url.get_secret_value()
        url = make_url(connection)
        sanitizer = TraceSanitizer({"password": url.password, "secret": connection})
        data = sanitizer.clean(cast(JsonValue, report.model_dump(mode="json")))
        emit(command, data, code, json_output)
        return code
    except SystemExit as exc:  # argparse help is a successful control operation.
        return int(exc.code or 0)
    except (
        InputError,
        ResourceNotFound,
        InvalidEvaluation,
        ExecutionConflict,
        ScoringConflict,
    ) as exc:
        emit(command, None, 2, json_output, str(exc), "invalid_input")
        return 2
    except (ValidationError, ArgumentError):
        emit(
            command,
            None,
            2,
            json_output,
            "Invalid configuration or threshold values",
            "invalid_input",
        )
        return 2
    except (Exception, KeyboardInterrupt):
        # The terminal boundary must not leak SQL, URLs, credentials, or raw stack traces.
        emit(
            command,
            None,
            3,
            json_output,
            "Operation failed; verify database availability and migrations",
            "operational_failure",
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
