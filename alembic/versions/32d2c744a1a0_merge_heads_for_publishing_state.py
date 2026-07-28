"""merge_heads_for_publishing_state

Revision ID: 32d2c744a1a0
Revises: 0ef2d79b649c, 110afa1d4e75
Create Date: 2025-06-06 21:19:48.898577

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "32d2c744a1a0"
down_revision: str | Sequence[str] | None = ("0ef2d79b649c", "110afa1d4e75")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
