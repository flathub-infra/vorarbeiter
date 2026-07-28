"""add_build_url_column

Revision ID: 847c28a01d2e
Revises: 91d4dfb81ed9
Create Date: 2025-04-15 15:11:07.047315

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "847c28a01d2e"
down_revision: str | None = "91d4dfb81ed9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("build_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "build_url")
