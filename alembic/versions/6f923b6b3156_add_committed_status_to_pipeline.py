"""add_committed_status_to_pipeline

Revision ID: 6f923b6b3156
Revises: af57b26773ab
Create Date: 2025-06-06 19:12:32.322325

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "6f923b6b3156"
down_revision: Union[str, None] = "af57b26773ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE pipelinestatus ADD VALUE 'committed'")


def downgrade() -> None:
    pass
