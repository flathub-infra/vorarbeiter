"""add build_diagnosis table

Revision ID: 60fda1297222
Revises: 04fd2fe503c8
Create Date: 2026-11-15 20:38:11.035503

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "60fda1297222"
down_revision: str | None = "04fd2fe503c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "build_diagnosis",
        sa.Column("pipeline_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "completed",
                "failed",
                "skipped",
                name="builddiagnosisstatus",
            ),
            nullable=False,
        ),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=50), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("request_ids", sa.JSON(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("comment_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipeline.id"]),
        sa.PrimaryKeyConstraint("pipeline_id"),
    )


def downgrade() -> None:
    op.drop_table("build_diagnosis")
    op.execute("DROP TYPE IF EXISTS builddiagnosisstatus")
