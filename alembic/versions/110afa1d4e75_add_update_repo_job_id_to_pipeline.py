"""add update_repo_job_id to pipeline

Revision ID: 110afa1d4e75
Revises: 6f923b6b3156
Create Date: 2025-06-06 19:10:18.968906

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "110afa1d4e75"
down_revision: Union[str, None] = "6f923b6b3156"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pipeline", sa.Column("update_repo_job_id", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pipeline", "update_repo_job_id")
