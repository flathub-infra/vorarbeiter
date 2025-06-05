"""add commit_job_id and publish_job_id to pipeline

Revision ID: f526d3657559
Revises: 8d35fea04133
Create Date: 2025-06-05 16:40:07.228373

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f526d3657559"
down_revision: Union[str, None] = "8d35fea04133"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("commit_job_id", sa.Integer(), nullable=True))
    op.add_column("pipeline", sa.Column("publish_job_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "publish_job_id")
    op.drop_column("pipeline", "commit_job_id")
