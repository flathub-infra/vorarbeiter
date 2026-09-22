"""scope build failure issues by repository

Revision ID: 346fcadf322a
Revises: 64c97755c434
Create Date: 2026-09-22 11:46:26.950814
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "346fcadf322a"
down_revision: str | None = "64c97755c434"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "build_failure_issue",
        sa.Column(
            "flat_manager_repo",
            sa.String(length=64),
            server_default="stable",
            nullable=False,
        ),
    )
    op.drop_index("ix_build_failure_issue_app_id", table_name="build_failure_issue")
    op.create_index(
        "ix_build_failure_issue_app_id_flat_manager_repo",
        "build_failure_issue",
        ["app_id", "flat_manager_repo"],
        unique=True,
    )
    op.alter_column(
        "build_failure_issue",
        "flat_manager_repo",
        server_default=None,
    )


def downgrade() -> None:
    bind = op.get_bind()
    non_stable_rows = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM build_failure_issue "
            "WHERE flat_manager_repo != 'stable'"
            ")"
        )
    ).scalar()
    if non_stable_rows:
        raise RuntimeError(
            "Cannot downgrade build failure issue tracking while non-stable rows exist"
        )
    op.drop_index(
        "ix_build_failure_issue_app_id_flat_manager_repo",
        table_name="build_failure_issue",
    )
    op.drop_column("build_failure_issue", "flat_manager_repo")
    op.create_index(
        "ix_build_failure_issue_app_id",
        "build_failure_issue",
        ["app_id"],
        unique=True,
    )
