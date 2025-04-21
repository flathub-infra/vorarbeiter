"""Add flat_manager_repo field to Pipeline model

Revision ID: 597b9981818d
Revises: 492c5c7ba9b4
Create Date: 2025-04-21 10:28:34.892646

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "597b9981818d"
down_revision: Union[str, None] = "492c5c7ba9b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "pipeline",
        sa.Column("flat_manager_repo", sa.String(64), nullable=True, index=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "flat_manager_repo")
