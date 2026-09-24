"""Typed relational records. Cross-scope references are constrained in PostgreSQL."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from traceforge.domain.status import CaseStatus, RunStatus


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Record(Base):
    __abstract__ = True
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=lambda: datetime.now(UTC)
    )


class Project(Record):
    __tablename__ = "projects"
    __table_args__ = (CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    agent_configurations: Mapped[list["AgentConfiguration"]] = relationship(
        back_populates="project", passive_deletes="all"
    )
    datasets: Mapped[list["EvaluationDataset"]] = relationship(
        back_populates="project", passive_deletes="all"
    )


class AgentConfiguration(Record):
    __tablename__ = "agent_configurations"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        UniqueConstraint("id", "project_id"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("provider IN ('fake', 'openai', 'gemini')", name="provider_supported"),
        CheckConstraint("jsonb_typeof(parameters) = 'object'", name="parameters_object"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(30), default="fake", server_default="fake")
    model_name: Mapped[str] = mapped_column(String(200))
    system_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    parameters: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    pricing: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    project: Mapped[Project] = relationship(back_populates="agent_configurations")


class EvaluationDataset(Record):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        UniqueConstraint("id", "project_id"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("schema_version = 1", name="schema_version_supported"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    schema_version: Mapped[int] = mapped_column(default=1, server_default="1")
    project: Mapped[Project] = relationship(back_populates="datasets")
    test_cases: Mapped[list["TestCase"]] = relationship(
        back_populates="dataset", passive_deletes="all"
    )


class TestCase(Record):
    __tablename__ = "test_cases"
    __table_args__ = (
        UniqueConstraint("dataset_id", "name"),
        UniqueConstraint("id", "dataset_id"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("jsonb_typeof(input) = 'object'", name="input_object"),
        CheckConstraint("jsonb_typeof(expectations) = 'array'", name="expectations_array"),
    )
    dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    input: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    expected_output: Mapped[JsonValue] = mapped_column(JSONB(none_as_null=False), nullable=False)
    expectations: Mapped[list[dict[str, JsonValue]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    dataset: Mapped[EvaluationDataset] = relationship(back_populates="test_cases")


class EvaluationRun(Record):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_configuration_id", "project_id"],
            ["agent_configurations.id", "agent_configurations.project_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_id", "project_id"],
            ["evaluation_datasets.id", "evaluation_datasets.project_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "dataset_id"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= COALESCE(started_at, created_at)",
            name="timestamp_order",
        ),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    agent_configuration_id: Mapped[UUID] = mapped_column(index=True)
    dataset_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus, name="run_status", values_callable=lambda enum: [item.value for item in enum]
        ),
        default=RunStatus.PENDING,
        server_default="pending",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    pricing_snapshot: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    scoring_version: Mapped[str | None] = mapped_column(String(50))
    case_results: Mapped[list["CaseResult"]] = relationship(
        back_populates="run", passive_deletes="all"
    )


class CaseResult(Record):
    __tablename__ = "case_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "dataset_id"],
            ["evaluation_runs.id", "evaluation_runs.dataset_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["test_case_id", "dataset_id"],
            ["test_cases.id", "test_cases.dataset_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "test_case_id"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0", name="total_tokens_nonnegative"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= COALESCE(started_at, created_at)",
            name="timestamp_order",
        ),
    )

    @property
    def evaluation_outcome(self) -> str:
        from traceforge.scoring.metrics import outcome

        return outcome(self)

    run_id: Mapped[UUID] = mapped_column(index=True)
    dataset_id: Mapped[UUID] = mapped_column()
    test_case_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[CaseStatus] = mapped_column(
        Enum(
            CaseStatus,
            name="case_status",
            values_callable=lambda enum: [item.value for item in enum],
        ),
        default=CaseStatus.PENDING,
        server_default="pending",
    )
    response: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    input_snapshot: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    provider: Mapped[str | None] = mapped_column(String(30))
    model_name: Mapped[str | None] = mapped_column(String(200))
    latency_ms: Mapped[float | None] = mapped_column()
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    total_tokens: Mapped[int | None] = mapped_column()
    error_type: Mapped[str | None] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expectations_snapshot: Mapped[list[dict[str, JsonValue]] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    run: Mapped[EvaluationRun] = relationship(back_populates="case_results")
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        passive_deletes="all", order_by="ToolCall.sequence"
    )
    scoring_results: Mapped[list["ScoringResult"]] = relationship(
        passive_deletes="all", order_by="ScoringResult.scorer_name"
    )
    events: Mapped[list["ExecutionEvent"]] = relationship(
        back_populates="case_result", passive_deletes="all", order_by="ExecutionEvent.sequence"
    )


class ToolCall(Record):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("case_result_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("jsonb_typeof(arguments) = 'object'", name="arguments_object"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="timestamp_order"),
    )
    case_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("case_results.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column()
    provider_call_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    output: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(60))
    arguments_validated: Mapped[bool] = mapped_column(default=False, server_default="false")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[float | None] = mapped_column()


class ExecutionEvent(Record):
    """One public model/tool observation in a case's total event order."""

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("case_result_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("latency_ms >= 0", name="latency_nonnegative"),
        CheckConstraint("finished_at >= started_at", name="timestamp_order"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0", name="total_tokens_nonnegative"
        ),
        CheckConstraint(
            "kind IN ('case_started', 'model_request', 'model_response', 'model_error', "
            "'tool_request', 'tool_result', 'error', 'case_completed')",
            name="kind_supported",
        ),
    )
    case_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("case_results.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column()
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[float] = mapped_column()
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    total_tokens: Mapped[int | None] = mapped_column()
    case_result: Mapped[CaseResult] = relationship(back_populates="events")


class ScoringResult(Record):
    __tablename__ = "scoring_results"
    __table_args__ = (
        UniqueConstraint("case_result_id", "scorer_name", "scorer_version"),
        CheckConstraint("value >= 0 AND value <= 1", name="value_normalized"),
        CheckConstraint("length(trim(scorer_name)) > 0", name="scorer_name_not_blank"),
        CheckConstraint("length(trim(scorer_version)) > 0", name="scorer_version_not_blank"),
    )
    case_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("case_results.id", ondelete="RESTRICT"), index=True
    )
    scorer_type: Mapped[str] = mapped_column(String(50), default="legacy", server_default="legacy")
    explanation: Mapped[str] = mapped_column(Text, default="", server_default="")
    expected: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    observed: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    scorer_name: Mapped[str] = mapped_column(String(200))
    scorer_version: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column()
    passed: Mapped[bool] = mapped_column()
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict, server_default="{}")
