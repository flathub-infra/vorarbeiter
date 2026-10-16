"""add actor to pipeline

Revision ID: 2cc7f32ee876
Revises: f0cdac19bd4f
Create Date: 2026-10-16 21:18:09.975860

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2cc7f32ee876"
down_revision: str | None = "f0cdac19bd4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pipeline", sa.Column("actor", sa.String(length=255), nullable=True))
    op.execute(
        "UPDATE pipeline SET actor = webhookevent.actor FROM webhookevent "
        "WHERE webhookevent.id = pipeline.webhook_event_id"
    )
    op.drop_constraint("pipeline_webhook_event_id_fkey", "pipeline", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        "pipeline_webhook_event_id_fkey",
        "pipeline",
        "webhookevent",
        ["webhook_event_id"],
        ["id"],
    )
    op.drop_column("pipeline", "actor")
