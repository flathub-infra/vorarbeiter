"""add build failure issue tracking

Revision ID: c57ac0fb8804
Revises: e6bd68df45cf
Create Date: 2026-08-11 16:31:44.815101

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c57ac0fb8804"
down_revision: str | None = "e6bd68df45cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "build_failure_issue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("app_id", sa.String(length=255), nullable=False),
        sa.Column("git_repo", sa.String(length=255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("latest_sha", sa.String(length=64), nullable=True),
        sa.Column("latest_build_url", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_build_failure_issue_app_id"),
        "build_failure_issue",
        ["app_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_build_failure_issue_app_id"),
        table_name="build_failure_issue",
    )
    op.drop_table("build_failure_issue")
