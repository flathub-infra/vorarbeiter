from datetime import datetime
from typing import Any

import attr
import structlog
from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.jobs import Job
from procrastinate.retry import RetryDecision
from sqlalchemy.ext.asyncio import AsyncSession

from app.queue import app

logger = structlog.get_logger(__name__)


@attr.dataclass(kw_only=True)
class QueueRetryStrategy(RetryStrategy):
    def get_retry_decision(
        self, *, exception: BaseException, job: Job
    ) -> RetryDecision | None:
        decision = super().get_retry_decision(exception=exception, job=job)
        if decision is None:
            return None
        retry_after = getattr(exception, "retry_after", None)
        if retry_after:
            decision = RetryDecision(retry_in={"seconds": retry_after})
        logger.warning(
            "Task retry scheduled",
            queue=job.queue,
            task=job.task_name,
            attempts=job.attempts,
            retry_at=decision.retry_at,
        )
        return decision


async def defer(
    db: AsyncSession,
    task: str,
    *,
    lock: str | None = None,
    schedule_at: datetime | None = None,
    **kwargs: Any,
) -> bool:
    options: dict[str, Any] = {}
    if lock is not None:
        options["lock"] = lock
        options["queueing_lock"] = lock
    if schedule_at is not None:
        options["schedule_at"] = schedule_at
    try:
        async with db.begin_nested():
            options["connection"] = (
                await (await db.connection()).get_raw_connection()
            ).driver_connection
            await app.configure_task(task, **options).defer_async(**kwargs)
    except AlreadyEnqueued:
        return False
    return True
