"""Remove repo field from Pipeline model

Revision ID: remove_repo_field
Revises: d4f3045fd7c0
Create Date: 2025-04-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "remove_repo_field"
down_revision: Union[str, None] = "d4f3045fd7c0"  # Update this to the latest migration
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the repo column
    op.drop_column("pipeline", "repo")


def downgrade() -> None:
    """Downgrade schema."""
    # Add the repo column back
    op.add_column(
        "pipeline",
        sa.Column("repo", sa.String(64), nullable=True, index=True),
    )
