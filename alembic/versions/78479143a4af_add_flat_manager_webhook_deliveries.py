"""add flat-manager webhook deliveries

Revision ID: 78479143a4af
Revises: 60fda1297222
Create Date: 2026-11-23 11:15:51.425715

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "78479143a4af"
down_revision: str | None = "60fda1297222"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "flat_manager_webhook_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_flat_manager_webhook_deliveries_received_at",
        "flat_manager_webhook_deliveries",
        ["received_at"],
    )
    op.create_index(
        "ix_flat_manager_webhook_deliveries_unprocessed",
        "flat_manager_webhook_deliveries",
        ["received_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.create_index("ix_pipeline_commit_job_id", "pipeline", ["commit_job_id"])
    op.create_index("ix_pipeline_publish_job_id", "pipeline", ["publish_job_id"])
    op.create_index(
        "ix_pipeline_update_repo_job_id", "pipeline", ["update_repo_job_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_update_repo_job_id", table_name="pipeline")
    op.drop_index("ix_pipeline_publish_job_id", table_name="pipeline")
    op.drop_index("ix_pipeline_commit_job_id", table_name="pipeline")
    op.drop_table("flat_manager_webhook_deliveries")
