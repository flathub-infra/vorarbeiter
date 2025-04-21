"""add_superseded_status

Revision ID: e2349ab06f32
Revises: e8199ca10595
Create Date: 2025-04-21 16:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2349ab06f32"
down_revision: str | None = "e8199ca10595"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE TYPE pipelinestatus_new AS ENUM ('pending', 'running', 'failed', 'cancelled', 'published', 'succeeded', 'superseded')"
    )

    op.execute(
        "ALTER TABLE pipeline ALTER COLUMN status TYPE pipelinestatus_new USING status::text::pipelinestatus_new"
    )

    op.execute("DROP TYPE pipelinestatus")
    op.execute("ALTER TYPE pipelinestatus_new RENAME TO pipelinestatus")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "CREATE TYPE pipelinestatus_old AS ENUM ('pending', 'running', 'failed', 'cancelled', 'published', 'succeeded')"
    )

    # In downgrade, any superseded pipelines will be converted to succeeded
    op.execute(
        "ALTER TABLE pipeline ALTER COLUMN status TYPE pipelinestatus_old USING CASE WHEN status::text = 'superseded' THEN 'succeeded'::pipelinestatus_old ELSE status::text::pipelinestatus_old END"
    )

    op.execute("DROP TYPE pipelinestatus")
    op.execute("ALTER TYPE pipelinestatus_old RENAME TO pipelinestatus")
