"""add_github_task_table

Revision ID: a1b2c3d4e5f6
Revises: 5bcc304afa74
Create Date: 2026-01-09 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "5bcc304afa74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "github_task",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "running", "completed", "failed", name="githubtaskstatus"
            ),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_github_task_id"), "github_task", ["id"], unique=False)
    op.create_index(
        op.f("ix_github_task_task_type"), "github_task", ["task_type"], unique=False
    )
    op.create_index(
        op.f("ix_github_task_status"), "github_task", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_github_task_next_attempt_at"),
        "github_task",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_github_task_created_at"), "github_task", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_github_task_created_at"), table_name="github_task")
    op.drop_index(op.f("ix_github_task_next_attempt_at"), table_name="github_task")
    op.drop_index(op.f("ix_github_task_status"), table_name="github_task")
    op.drop_index(op.f("ix_github_task_task_type"), table_name="github_task")
    op.drop_index(op.f("ix_github_task_id"), table_name="github_task")
    op.drop_table("github_task")
    op.execute("DROP TYPE IF EXISTS githubtaskstatus")
