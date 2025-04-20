"""remove_result_field

Revision ID: e8199ca10595
Revises: 847c28a01d2e
Create Date: 2025-04-20 08:52:01.817815

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e8199ca10595"
down_revision: Union[str, None] = "847c28a01d2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("pipeline", "result")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("pipeline", sa.Column("result", sa.JSON, nullable=True))
