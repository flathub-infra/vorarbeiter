"""add procrastinate schema

Revision ID: 04fd2fe503c8
Revises: e284af035b32
Create Date: 2026-11-15 20:32:14.666619

"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "04fd2fe503c8"
down_revision: str | None = "e284af035b32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = Path(__file__).resolve().parents[1] / "sql" / "procrastinate_3.11.0.sql"


def upgrade() -> None:
    conn = op.get_bind().connection.driver_connection
    assert conn is not None
    conn.execute(SCHEMA.read_text())


def downgrade() -> None:
    conn = op.get_bind().connection.driver_connection
    assert conn is not None
    conn.execute(
        "DROP TABLE IF EXISTS procrastinate_events, procrastinate_periodic_defers, "
        "procrastinate_jobs, procrastinate_workers CASCADE"
    )
    rows = conn.execute(
        "SELECT 'DROP FUNCTION ' || oid::regprocedure || ' CASCADE' FROM pg_proc "
        "WHERE proname LIKE 'procrastinate\\_%' AND pronamespace = 'public'::regnamespace"
    ).fetchall()
    for (stmt,) in rows:
        conn.execute(stmt)
    conn.execute(
        "DROP TYPE IF EXISTS procrastinate_job_event_type, procrastinate_job_status, "
        "procrastinate_job_to_defer_v1 CASCADE"
    )
