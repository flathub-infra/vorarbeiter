"""Add is_extra_data column to pipeline table

Revision ID: 4e5ef8b2fca8
Revises: ca4c8c37fa88
Create Date: 2025-04-28 10:57:56.542440

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4e5ef8b2fca8"
down_revision: Union[str, None] = "ca4c8c37fa88"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("is_extra_data", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "is_extra_data")
