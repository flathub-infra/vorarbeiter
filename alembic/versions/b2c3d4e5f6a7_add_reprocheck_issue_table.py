"""add_reprocheck_issue_table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-19 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reprocheck_issue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("app_id", sa.String(length=255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_reprocheck_issue_app_id"),
        "reprocheck_issue",
        ["app_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_reprocheck_issue_app_id"), table_name="reprocheck_issue")
    op.drop_table("reprocheck_issue")
