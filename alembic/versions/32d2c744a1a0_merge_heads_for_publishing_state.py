"""merge_heads_for_publishing_state

Revision ID: 32d2c744a1a0
Revises: 0ef2d79b649c, 110afa1d4e75
Create Date: 2025-06-06 21:19:48.898577

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "32d2c744a1a0"
down_revision: Union[str, Sequence[str], None] = ("0ef2d79b649c", "110afa1d4e75")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
