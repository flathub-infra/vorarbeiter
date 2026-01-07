"""add_total_cost_to_pipeline

Revision ID: 5bcc304afa74
Revises: 2b058eab246e
Create Date: 2026-01-07 12:34:49.801373

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5bcc304afa74"
down_revision: Union[str, None] = "2b058eab246e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("total_cost", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "total_cost")
