"""drop webhookevent

Revision ID: e284af035b32
Revises: 2cc7f32ee876
Create Date: 2026-10-16 21:41:12.512918

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e284af035b32"
down_revision: str | None = "2cc7f32ee876"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE pipeline SET actor = webhookevent.actor FROM webhookevent "
        "WHERE webhookevent.id = pipeline.webhook_event_id AND pipeline.actor IS NULL"
    )
    op.drop_column("pipeline", "webhook_event_id")
    op.drop_table("webhookevent")
    op.execute("DROP TYPE webhooksource")


def downgrade() -> None:
    op.create_table(
        "webhookevent",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Enum("GITHUB", name="webhooksource"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("actor", "id", "processed", "received_at", "repository", "source"):
        op.create_index(
            op.f(f"ix_webhookevent_{column}"), "webhookevent", [column], unique=False
        )
    op.add_column("pipeline", sa.Column("webhook_event_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_pipeline_webhook_event_id"),
        "pipeline",
        ["webhook_event_id"],
        unique=False,
    )
