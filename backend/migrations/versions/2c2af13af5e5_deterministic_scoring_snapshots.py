"""deterministic scoring snapshots

Revision ID: 2c2af13af5e5
Revises: 0fe7f845cf6f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2c2af13af5e5"
down_revision: str | None = "0fe7f845cf6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_configurations",
        sa.Column(
            "pricing", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "case_results",
        sa.Column(
            "expectations_snapshot",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("case_results", sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "case_results",
        sa.Column("estimated_cost_usd", sa.Numeric(precision=38, scale=18), nullable=True),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column(
            "pricing_snapshot",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "evaluation_runs", sa.Column("scoring_version", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "scoring_results",
        sa.Column("scorer_type", sa.String(length=50), server_default="legacy", nullable=False),
    )
    op.add_column(
        "scoring_results", sa.Column("explanation", sa.Text(), server_default="", nullable=False)
    )
    op.add_column(
        "scoring_results",
        sa.Column("expected", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "scoring_results",
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "test_cases",
        sa.Column(
            "expectations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )

    op.create_check_constraint(
        op.f("ck_test_cases_expectations_array"),
        "test_cases",
        "jsonb_typeof(expectations) = 'array'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_test_cases_expectations_array"), "test_cases", type_="check")
    op.drop_column("test_cases", "expectations")
    op.drop_column("scoring_results", "observed")
    op.drop_column("scoring_results", "expected")
    op.drop_column("scoring_results", "explanation")
    op.drop_column("scoring_results", "scorer_type")
    op.drop_column("evaluation_runs", "scoring_version")
    op.drop_column("evaluation_runs", "pricing_snapshot")
    op.drop_column("case_results", "estimated_cost_usd")
    op.drop_column("case_results", "scored_at")
    op.drop_column("case_results", "expectations_snapshot")
    op.drop_column("agent_configurations", "pricing")
