"""add procrastinate schema

Revision ID: 04fd2fe503c8
Revises: e284af035b32
Create Date: 2026-11-15 20:32:14.666619

"""

from collections.abc import Sequence

revision: str = "04fd2fe503c8"
down_revision: str | None = "e284af035b32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "procrastinate_0038"


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
