"""add_build_url_column

Revision ID: 847c28a01d2e
Revises: 91d4dfb81ed9
Create Date: 2025-04-15 15:11:07.047315

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "847c28a01d2e"
down_revision: Union[str, None] = "91d4dfb81ed9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("pipeline", sa.Column("build_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pipeline", "build_url")
