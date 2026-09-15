from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "64c97755c434"
down_revision: str | None = "c57ac0fb8804"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inactive_repo_snapshot",
        sa.Column("organization", sa.String(length=255), nullable=False),
        sa.Column("scan_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scan_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("automatic_candidates", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("organization"),
    )


def downgrade() -> None:
    op.drop_table("inactive_repo_snapshot")
