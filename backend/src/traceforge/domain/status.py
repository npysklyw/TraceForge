"""Execution states. Scoring belongs to ScoringResult, not new case transitions."""

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    # Read-only compatibility with the initial persistence schema.
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    allowed = {
        RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.CANCELLED},
        RunStatus.RUNNING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
    }
    if target not in allowed.get(current, set()):
        raise ValueError(f"Cannot transition run from {current} to {target}")


def validate_case_transition(current: CaseStatus, target: CaseStatus) -> None:
    allowed = {
        CaseStatus.PENDING: {CaseStatus.RUNNING, CaseStatus.SKIPPED},
        CaseStatus.RUNNING: {
            CaseStatus.COMPLETED,
            CaseStatus.ERROR,
        },
    }
    if target not in allowed.get(current, set()):
        raise ValueError(f"Cannot transition case from {current} to {target}")
