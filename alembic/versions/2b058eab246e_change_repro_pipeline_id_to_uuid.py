"""change_repro_pipeline_id_to_uuid

Revision ID: 2b058eab246e
Revises: b1b25eccbe2a
Create Date: 2025-07-24 17:13:06.391124

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2b058eab246e"
down_revision: Union[str, None] = "b1b25eccbe2a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("pipeline", "repro_pipeline_id")
    op.add_column("pipeline", sa.Column("repro_pipeline_id", sa.UUID(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "repro_pipeline_id")
    op.add_column(
        "pipeline", sa.Column("repro_pipeline_id", sa.Integer(), nullable=True)
    )
