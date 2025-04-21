"""Add repo field to Pipeline model

Revision ID: 492c5c7ba9b4
Revises: e8199ca10595
Create Date: 2025-04-21 09:02:46.956615

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "492c5c7ba9b4"
down_revision: Union[str, None] = "e8199ca10595"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "pipeline",
        sa.Column("repo", sa.String(64), nullable=True, index=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "repo")
