import pytest
from pydantic import ValidationError

from traceforge.api.schemas import (
    AgentConfigurationCreate,
    ProjectCreate,
    ProjectUpdate,
    ScoringResultCreate,
)
from traceforge.api.schemas import (
    TestCaseCreate as CaseCreate,
)
from traceforge.api.schemas import (
    TestCaseUpdate as CaseUpdate,
)
from traceforge.domain.status import (
    CaseStatus,
    RunStatus,
    validate_case_transition,
    validate_run_transition,
)


@pytest.mark.parametrize(
    "current,target",
    [
        (RunStatus.PENDING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.PENDING, RunStatus.CANCELLED),
    ],
)
def test_run_transitions(current, target):
    validate_run_transition(current, target)


@pytest.mark.parametrize("current", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
def test_terminal_runs_cannot_restart(current):
    with pytest.raises(ValueError, match="Cannot transition"):
        validate_run_transition(current, RunStatus.RUNNING)


def test_case_execution_is_separate_from_scoring():
    validate_case_transition(CaseStatus.PENDING, CaseStatus.RUNNING)
    for target in (CaseStatus.COMPLETED, CaseStatus.ERROR):
        validate_case_transition(CaseStatus.RUNNING, target)
    for legacy_score_status in (CaseStatus.PASSED, CaseStatus.FAILED):
        with pytest.raises(ValueError):
            validate_case_transition(CaseStatus.RUNNING, legacy_score_status)
    validate_case_transition(CaseStatus.PENDING, CaseStatus.SKIPPED)
    with pytest.raises(ValueError):
        validate_case_transition(CaseStatus.PASSED, CaseStatus.RUNNING)


@pytest.mark.parametrize("name", ["", "  ", "a" * 201])
def test_invalid_names(name):
    with pytest.raises(ValidationError):
        ProjectCreate(name=name)


def test_names_trimmed_without_modifying_prompt():
    agent = AgentConfigurationCreate(name="  demo  ", system_prompt="  Preserve whitespace\n")
    assert agent.name == "demo"
    assert agent.system_prompt == "  Preserve whitespace\n"
    assert agent.provider == "fake"


@pytest.mark.parametrize("payload", [{}, {"name": None}, {"unknown": "value"}])
def test_invalid_patches(payload):
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate(payload)


def test_expected_json_null_is_distinct_from_omission():
    assert CaseUpdate(expected_output=None).model_dump(exclude_unset=True) == {
        "expected_output": None
    }
    assert CaseUpdate(name="rename").model_dump(exclude_unset=True) == {"name": "rename"}
    assert CaseCreate(name="nullable", input={}, expected_output=None).expected_output is None


def test_payload_limit():
    with pytest.raises(ValidationError, match="256 KiB"):
        CaseCreate(name="oversized", input={"data": "x" * 262144}, expected_output=None)


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_normalized_scores(value):
    from uuid import uuid4

    with pytest.raises(ValidationError):
        ScoringResultCreate(
            case_result_id=uuid4(),
            scorer_name="exact_match",
            scorer_version="1",
            value=value,
            passed=False,
        )
