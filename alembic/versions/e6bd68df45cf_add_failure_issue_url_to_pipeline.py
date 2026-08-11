"""add failure issue url to pipeline

Revision ID: e6bd68df45cf
Revises: 694656b4a343
Create Date: 2026-08-11 15:56:55.617981

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6bd68df45cf"
down_revision: str | None = "694656b4a343"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("failure_issue_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "failure_issue_url")
