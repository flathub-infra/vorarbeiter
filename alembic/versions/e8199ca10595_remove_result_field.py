"""remove_result_field

Revision ID: e8199ca10595
Revises: 847c28a01d2e
Create Date: 2025-04-20 08:52:01.817815

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8199ca10595"
down_revision: str | None = "847c28a01d2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("pipeline", "result")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("pipeline", sa.Column("result", sa.JSON, nullable=True))
