"""add review_required to pipeline

Revision ID: f0cdac19bd4f
Revises: 6ab8fdef41f8
Create Date: 2026-10-11 22:59:07.106343

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0cdac19bd4f"
down_revision: str | None = "6ab8fdef41f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline",
        sa.Column(
            "review_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("pipeline", "review_required")
