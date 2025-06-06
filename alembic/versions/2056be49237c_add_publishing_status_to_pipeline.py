"""add publishing status to pipeline

Revision ID: 2056be49237c
Revises: 32d2c744a1a0
Create Date: 2025-06-06 19:23:31.191468

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "2056be49237c"
down_revision: Union[str, None] = "32d2c744a1a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'publishing' to the enum type
    op.execute("ALTER TYPE pipelinestatus ADD VALUE 'publishing' AFTER 'committed'")


def downgrade() -> None:
    # Note: Removing enum values in PostgreSQL is complex and requires recreating the type
    # This is a simplified version that may not work in all cases
    pass
