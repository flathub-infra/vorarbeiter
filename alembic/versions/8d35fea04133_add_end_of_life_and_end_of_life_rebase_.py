"""Add end_of_life and end_of_life_rebase fields

Revision ID: 8d35fea04133
Revises: 4e5ef8b2fca8
Create Date: 2025-06-05 11:14:43.481764

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8d35fea04133"
down_revision: Union[str, None] = "4e5ef8b2fca8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("end_of_life", sa.String(), nullable=True))
    op.add_column(
        "pipeline", sa.Column("end_of_life_rebase", sa.String(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "end_of_life_rebase")
    op.drop_column("pipeline", "end_of_life")
