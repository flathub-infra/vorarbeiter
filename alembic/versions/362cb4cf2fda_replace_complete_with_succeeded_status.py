"""replace_complete_with_succeeded_status

Revision ID: 362cb4cf2fda
Revises: 0be7e58777c1
Create Date: 2025-04-11 21:32:49.647750

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "362cb4cf2fda"
down_revision: Union[str, None] = "0be7e58777c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE TYPE pipelinestatus_new AS ENUM ('pending', 'running', 'failed', 'cancelled', 'published', 'succeeded')"
    )

    op.execute(
        "ALTER TABLE pipeline ALTER COLUMN status TYPE pipelinestatus_new USING CASE WHEN status::text = 'complete' THEN 'succeeded'::pipelinestatus_new ELSE status::text::pipelinestatus_new END"
    )

    op.execute("DROP TYPE pipelinestatus")
    op.execute("ALTER TYPE pipelinestatus_new RENAME TO pipelinestatus")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "CREATE TYPE pipelinestatus_old AS ENUM ('pending', 'running', 'complete', 'failed', 'cancelled', 'published')"
    )

    op.execute(
        "ALTER TABLE pipeline ALTER COLUMN status TYPE pipelinestatus_old USING CASE WHEN status::text = 'succeeded' THEN 'complete'::pipelinestatus_old ELSE status::text::pipelinestatus_old END"
    )

    op.execute("DROP TYPE pipelinestatus")
    op.execute("ALTER TYPE pipelinestatus_old RENAME TO pipelinestatus")
