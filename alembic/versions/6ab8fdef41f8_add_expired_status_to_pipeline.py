"""add expired status to pipeline

Revision ID: 6ab8fdef41f8
Revises: 346fcadf322a
Create Date: 2026-10-08 14:07:06.394696

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6ab8fdef41f8"
down_revision: str | None = "346fcadf322a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE pipelinestatus ADD VALUE 'expired'")


def downgrade() -> None:
    pass
