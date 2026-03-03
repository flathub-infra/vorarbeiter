"""remove_is_extra_data_from_pipeline

Revision ID: e06d44b78199
Revises: b2c3d4e5f6a7
Create Date: 2026-03-03 14:03:31.348429

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e06d44b78199"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("pipeline", "is_extra_data")


def downgrade() -> None:
    op.add_column("pipeline", sa.Column("is_extra_data", sa.Boolean(), nullable=True))
