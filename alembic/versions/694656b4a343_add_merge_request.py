"""add_merge_request

Revision ID: 694656b4a343
Revises: e06d44b78199
Create Date: 2026-04-21 10:55:41.372317

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "694656b4a343"
down_revision: Union[str, None] = "e06d44b78199"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merge_request",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("app_id", sa.String(length=255), nullable=False),
        sa.Column("target_branch", sa.String(length=255), nullable=False),
        sa.Column("pr_head_sha", sa.String(length=40), nullable=False),
        sa.Column("collaborators", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "creating",
                "pushing",
                "finalizing",
                "completed",
                "failed",
                name="mergestatus",
            ),
            nullable=False,
        ),
        sa.Column("callback_token", sa.String(length=32), nullable=False),
        sa.Column("comment_author", sa.String(length=255), nullable=False),
        sa.Column("fork_url", sa.Text(), nullable=False),
        sa.Column("fork_branch", sa.String(length=255), nullable=False),
        sa.Column("repo_html_url", sa.Text(), nullable=True),
        sa.Column("provider_data", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_merge_request_id"), "merge_request", ["id"], unique=False)
    op.create_index(
        op.f("ix_merge_request_pr_number"),
        "merge_request",
        ["pr_number"],
        unique=False,
    )
    op.create_index(
        "uq_merge_request_active_pr_number",
        "merge_request",
        ["pr_number"],
        unique=True,
        postgresql_where=sa.text("status IN ('creating', 'pushing', 'finalizing')"),
    )
    op.create_index(
        op.f("ix_merge_request_app_id"), "merge_request", ["app_id"], unique=False
    )
    op.create_index(
        op.f("ix_merge_request_status"), "merge_request", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_merge_request_created_at"),
        "merge_request",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_merge_request_created_at"), table_name="merge_request")
    op.drop_index(op.f("ix_merge_request_status"), table_name="merge_request")
    op.drop_index(op.f("ix_merge_request_app_id"), table_name="merge_request")
    op.drop_index("uq_merge_request_active_pr_number", table_name="merge_request")
    op.drop_index(op.f("ix_merge_request_pr_number"), table_name="merge_request")
    op.drop_index(op.f("ix_merge_request_id"), table_name="merge_request")
    op.drop_table("merge_request")
    op.execute("DROP TYPE IF EXISTS mergestatus")
