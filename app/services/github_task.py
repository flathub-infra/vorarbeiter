from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.github_task import GitHubTask, GitHubTaskStatus
from app.utils.github import get_github_client

logger = structlog.get_logger(__name__)


class GitHubTaskService:
    def calculate_next_attempt(
        self, attempt_count: int, retry_after: float | None = None
    ) -> datetime:
        if retry_after and retry_after > 0:
            return datetime.now(timezone.utc) + timedelta(seconds=retry_after)

        base_delay = 60
        max_delay = 3600
        delay = min(base_delay * (2**attempt_count), max_delay)

        return datetime.now(timezone.utc) + timedelta(seconds=delay)

    async def queue_task(
        self,
        db: AsyncSession,
        task_type: str,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> GitHubTask:
        next_attempt = self.calculate_next_attempt(0, retry_after)

        task = GitHubTask(
            task_type=task_type,
            method=method,
            url=url,
            payload=payload,
            context=context,
            next_attempt_at=next_attempt,
        )
        db.add(task)
        await db.flush()

        logger.info(
            "Queued GitHub API task for retry",
            task_id=str(task.id),
            task_type=task_type,
            url=url,
            next_attempt_at=next_attempt.isoformat(),
        )

        return task

    async def process_pending_tasks(self, db: AsyncSession, limit: int = 50) -> int:
        now = datetime.now(timezone.utc)

        stmt = (
            select(GitHubTask)
            .where(GitHubTask.status == GitHubTaskStatus.PENDING)
            .where(GitHubTask.next_attempt_at <= now)
            .order_by(GitHubTask.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        db_result = await db.execute(stmt)
        tasks = db_result.scalars().all()

        processed = 0
        client = get_github_client()

        for task in tasks:
            task.status = GitHubTaskStatus.RUNNING
            task.attempt_count += 1
            await db.flush()

            try:
                result = await client.request_with_result(
                    task.method,
                    task.url,
                    json=task.payload,
                    context=task.context or {},
                    max_retries=0,
                )

                if result.response:
                    task.status = GitHubTaskStatus.COMPLETED
                    task.completed_at = datetime.now(timezone.utc)
                    logger.info(
                        "GitHub task completed successfully",
                        task_id=str(task.id),
                        task_type=task.task_type,
                    )
                else:
                    self._handle_task_failure(
                        task, result.error_type or "Request failed", result.retry_after
                    )

            except Exception as e:
                self._handle_task_failure(task, str(e))

            processed += 1

        return processed

    def _handle_task_failure(
        self, task: GitHubTask, error: str, retry_after: float | None = None
    ) -> None:
        task.last_error = error

        if task.attempt_count >= task.max_attempts:
            task.status = GitHubTaskStatus.FAILED
            task.completed_at = datetime.now(timezone.utc)
            logger.error(
                "GitHub task failed permanently",
                task_id=str(task.id),
                task_type=task.task_type,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
                last_error=error,
            )
        else:
            task.status = GitHubTaskStatus.PENDING
            task.next_attempt_at = self.calculate_next_attempt(
                task.attempt_count, retry_after
            )
            logger.warning(
                "GitHub task failed, will retry",
                task_id=str(task.id),
                task_type=task.task_type,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
                next_attempt_at=task.next_attempt_at.isoformat(),
                last_error=error,
            )

    async def cleanup_old_tasks(self, db: AsyncSession, days: int = 7) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        stmt = (
            select(GitHubTask)
            .where(
                GitHubTask.status.in_(
                    [GitHubTaskStatus.COMPLETED, GitHubTaskStatus.FAILED]
                )
            )
            .where(GitHubTask.completed_at < cutoff)
        )
        result = await db.execute(stmt)
        tasks = result.scalars().all()

        count = len(tasks)
        for task in tasks:
            await db.delete(task)

        if count > 0:
            logger.info(
                "Cleaned up old GitHub tasks", count=count, older_than_days=days
            )

        return count
