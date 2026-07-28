"""add repro_pipeline_id to pipeline

Revision ID: b1b25eccbe2a
Revises: 2056be49237c
Create Date: 2025-07-24 12:34:39.706954

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1b25eccbe2a"
down_revision: str | None = "2056be49237c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline", sa.Column("repro_pipeline_id", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pipeline", "repro_pipeline_id")
