"""merge heads

Revision ID: d4f3045fd7c0
Revises: e2349ab06f32, 597b9981818d
Create Date: 2025-04-21 19:48:29.191727

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d4f3045fd7c0"
down_revision: str | tuple[str, ...] | None = ("e2349ab06f32", "597b9981818d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
