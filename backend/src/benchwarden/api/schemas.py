from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from benchwarden.application.redaction import TraceSanitizer
from benchwarden.domain.status import CaseStatus, RunStatus
from benchwarden.scoring.expectations import Expectation, Pricing, parse_expectations

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(max_length=10000)]
Prompt = Annotated[str, StringConstraints(max_length=100000)]
Provider = Literal["fake", "openai", "gemini"]


def validate_postgres_text(value: object) -> None:
    """PostgreSQL text and JSONB cannot represent the Unicode null character."""
    if isinstance(value, str) and "\x00" in value:
        raise ValueError("Text and JSON values cannot contain the Unicode null character")
    if isinstance(value, dict):
        for key, item in value.items():
            validate_postgres_text(key)
            validate_postgres_text(item)
    elif isinstance(value, list):
        for item in value:
            validate_postgres_text(item)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def bound_payload(self) -> Self:
        if hasattr(self, "expectations") and self.expectations is not None:
            parse_expectations(self.expectations)
        validate_postgres_text(self.model_dump())
        if len(self.model_dump_json().encode()) > 262144:
            raise ValueError("Record payload must not exceed 256 KiB")
        payload = self.model_dump(mode="json")
        if TraceSanitizer(payload).object(payload) != payload:
            raise ValueError(
                "Remove credentials, secrets, or internal-reasoning metadata before saving"
            )
        return self


class Patch(Request):
    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        for field in self.model_fields_set - {"expected_output", "pricing"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class RecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class ProjectCreate(Request):
    name: Name
    description: Description = ""


class ProjectUpdate(Patch):
    name: Name | None = None
    description: Description | None = None


class ProjectResponse(RecordResponse):
    name: str
    description: str


class AgentConfigurationCreate(Request):
    pricing: Pricing | None = None
    name: Name
    provider: Provider = "fake"
    model_name: Name = "deterministic-v1"
    system_prompt: Prompt = ""
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class AgentConfigurationUpdate(Patch):
    pricing: Pricing | None = None
    name: Name | None = None
    provider: Provider | None = None
    model_name: Name | None = None
    system_prompt: Prompt | None = None
    parameters: dict[str, JsonValue] | None = None


class AgentConfigurationResponse(RecordResponse):
    pricing: Pricing | None
    project_id: UUID
    name: str
    provider: Provider
    model_name: str
    system_prompt: str
    parameters: dict[str, JsonValue]


class DatasetCreate(Request):
    name: Name
    description: Description = ""
    schema_version: Literal[1] = 1


class DatasetUpdate(Patch):
    name: Name | None = None
    description: Description | None = None


class DatasetResponse(RecordResponse):
    project_id: UUID
    name: str
    description: str
    schema_version: int


class TestCaseCreate(Request):
    expectations: list[Expectation] = Field(default_factory=list, max_length=64)
    name: Name
    input: dict[str, JsonValue]
    expected_output: JsonValue


class TestCaseUpdate(Patch):
    expectations: list[Expectation] | None = Field(default=None, max_length=64)
    name: Name | None = None
    input: dict[str, JsonValue] | None = None
    expected_output: JsonValue = None


class TestCaseResponse(RecordResponse):
    expectations: list[Expectation]
    dataset_id: UUID
    name: str
    input: dict[str, JsonValue]
    expected_output: JsonValue


class EvaluationRunCreate(Request):
    project_id: UUID
    agent_configuration_id: UUID
    dataset_id: UUID


class EvaluationRunResponse(RecordResponse):
    pricing_snapshot: Pricing | None
    scoring_version: str | None
    project_id: UUID
    agent_configuration_id: UUID
    dataset_id: UUID
    status: RunStatus
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    error: str | None


class CaseResultCreate(Request):
    run_id: UUID
    dataset_id: UUID
    test_case_id: UUID


class CaseResultResponse(RecordResponse):
    evaluation_outcome: Literal["passed", "failed", "not_scored"]
    expectations_snapshot: list[Expectation] | None
    scored_at: AwareDatetime | None
    estimated_cost_usd: Decimal | None
    run_id: UUID
    dataset_id: UUID
    test_case_id: UUID
    status: CaseStatus
    response: JsonValue
    error: str | None
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    input_snapshot: dict[str, JsonValue]
    provider: str | None
    model_name: str | None
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_type: str | None


class ToolCallCreate(Request):
    case_result_id: UUID
    sequence: Annotated[int, Field(ge=0)]
    provider_call_id: Name | None = None
    name: Name
    arguments: dict[str, JsonValue]
    output: JsonValue = None
    error: Description | None = None


class ToolCallResponse(RecordResponse):
    case_result_id: UUID
    sequence: int
    provider_call_id: str | None
    name: str
    arguments: dict[str, JsonValue]
    output: JsonValue
    error: str | None
    error_type: str | None
    arguments_validated: bool
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    latency_ms: float | None


class ExecutionEventResponse(RecordResponse):
    sequence: int
    kind: str
    payload: dict[str, JsonValue]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class ResultDetail(CaseResultResponse):
    scoring_results: list["ScoringResultResponse"]
    events: list[ExecutionEventResponse]
    tool_calls: list[ToolCallResponse]


class RunDetail(EvaluationRunResponse):
    case_counts: dict[str, int]
    total_cases: int


class ScoringResultCreate(Request):
    case_result_id: UUID
    scorer_name: Name
    scorer_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
    ]
    value: Annotated[float, Field(ge=0, le=1)]
    passed: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ScoringResultResponse(RecordResponse):
    scorer_type: str
    explanation: str
    expected: JsonValue
    observed: JsonValue
    case_result_id: UUID
    scorer_name: str
    scorer_version: str
    value: float
    passed: bool
    details: dict[str, JsonValue]
