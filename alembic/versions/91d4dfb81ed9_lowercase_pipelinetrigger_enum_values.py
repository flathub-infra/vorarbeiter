"""lowercase_pipelinetrigger_enum_values

Revision ID: 91d4dfb81ed9
Revises: 362cb4cf2fda
Create Date: 2025-04-14 14:27:43.323215

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "91d4dfb81ed9"
down_revision: Union[str, None] = "362cb4cf2fda"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE TYPE pipelinetrigger_new AS ENUM ('webhook', 'manual')")

    op.execute(
        """
        ALTER TABLE pipeline ALTER COLUMN triggered_by TYPE pipelinetrigger_new 
        USING CASE 
            WHEN triggered_by::text = 'WEBHOOK' THEN 'webhook'::pipelinetrigger_new
            WHEN triggered_by::text = 'MANUAL' THEN 'manual'::pipelinetrigger_new
            ELSE triggered_by::text::pipelinetrigger_new 
        END
        """
    )

    op.execute("DROP TYPE pipelinetrigger")
    op.execute("ALTER TYPE pipelinetrigger_new RENAME TO pipelinetrigger")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("CREATE TYPE pipelinetrigger_old AS ENUM ('WEBHOOK', 'MANUAL')")

    op.execute(
        """
        ALTER TABLE pipeline ALTER COLUMN triggered_by TYPE pipelinetrigger_old 
        USING CASE 
            WHEN triggered_by::text = 'webhook' THEN 'WEBHOOK'::pipelinetrigger_old
            WHEN triggered_by::text = 'manual' THEN 'MANUAL'::pipelinetrigger_old
            ELSE triggered_by::text::pipelinetrigger_old 
        END
        """
    )

    op.execute("DROP TYPE pipelinetrigger")
    op.execute("ALTER TYPE pipelinetrigger_old RENAME TO pipelinetrigger")
