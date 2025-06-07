"""change_build_id_from_text_to_integer

Revision ID: 0ef2d79b649c
Revises: 6f923b6b3156
Create Date: 2025-06-06 20:58:47.590618

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0ef2d79b649c"
down_revision: Union[str, None] = "6f923b6b3156"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE pipeline SET build_id = NULL WHERE build_id IS NOT NULL AND build_id !~ '^[0-9]+$'"
    )
    op.execute(
        "ALTER TABLE pipeline ALTER COLUMN build_id TYPE INTEGER USING build_id::integer"
    )


def downgrade() -> None:
    op.alter_column(
        "pipeline",
        "build_id",
        existing_type=sa.Integer(),
        type_=sa.Text(),
        existing_nullable=True,
    )
