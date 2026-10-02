"""Store application-check reports independently of build status.

Revision ID: 971d2e0a4c83
Revises: 64c97755c434
"""

import sqlalchemy as sa

from alembic import op

revision = "971d2e0a4c83"
down_revision = "64c97755c434"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "smoke_result",
        sa.Column(
            "pipeline_id", sa.Uuid(), sa.ForeignKey("pipeline.id"), primary_key=True
        ),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column("source_attempt", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("notified", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_smoke_result_run_id", "smoke_result", ["run_id"])


def downgrade():
    op.drop_table("smoke_result")
