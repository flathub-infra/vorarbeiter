from datetime import datetime
from typing import Any

from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy.ext.asyncio import AsyncSession

from app.queue import app


async def defer(
    db: AsyncSession,
    task: str,
    *,
    lock: str | None = None,
    schedule_at: datetime | None = None,
    **kwargs: Any,
) -> bool:
    options: dict[str, Any] = {
        "connection": (
            await (await db.connection()).get_raw_connection()
        ).driver_connection
    }
    if lock is not None:
        options["lock"] = lock
        options["queueing_lock"] = lock
    if schedule_at is not None:
        options["schedule_at"] = schedule_at
    try:
        async with db.begin_nested():
            await app.configure_task(task, **options).defer_async(**kwargs)
    except AlreadyEnqueued:
        return False
    return True
