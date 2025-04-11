"""merge_job_into_pipeline

Revision ID: 2d6423ed97db
Revises: d582c098ac1e
Create Date: 2025-04-11 11:55:02.243406

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2d6423ed97db"
down_revision: Union[str, None] = "d582c098ac1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "pipeline", sa.Column("provider", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "pipeline",
        sa.Column("provider_data", sa.JSON(), nullable=True, server_default="{}"),
    )
    op.add_column("pipeline", sa.Column("result", sa.JSON(), nullable=True))
    op.create_index(
        op.f("ix_pipeline_provider"), "pipeline", ["provider"], unique=False
    )

    op.drop_index(op.f("ix_job_status"), table_name="job")
    op.drop_index(op.f("ix_job_provider"), table_name="job")
    op.drop_index(op.f("ix_job_position"), table_name="job")
    op.drop_index(op.f("ix_job_pipeline_id"), table_name="job")
    op.drop_index(op.f("ix_job_job_type"), table_name="job")
    op.drop_index(op.f("ix_job_id"), table_name="job")
    op.drop_index(op.f("ix_job_created_at"), table_name="job")
    op.drop_table("job")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_pipeline_provider"), table_name="pipeline")
    op.drop_column("pipeline", "result")
    op.drop_column("pipeline", "provider_data")
    op.drop_column("pipeline", "provider")
