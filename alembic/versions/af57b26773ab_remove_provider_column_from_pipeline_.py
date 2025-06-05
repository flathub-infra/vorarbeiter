"""remove_provider_column_from_pipeline_and_job

Revision ID: af57b26773ab
Revises: f526d3657559
Create Date: 2025-06-05 18:18:54.417752

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "af57b26773ab"
down_revision: Union[str, None] = "f526d3657559"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Remove provider column from pipeline table
    op.drop_column("pipeline", "provider")


def downgrade() -> None:
    """Downgrade schema."""
    # Add back provider column to pipeline table
    op.add_column("pipeline", sa.Column("provider", sa.String(255), nullable=True))
