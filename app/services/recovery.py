import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import aliased

from app.database import get_db
from app.models import (
    FlatManagerWebhookDelivery,
    Pipeline,
    PipelineStatus,
)
from app.services import flat_manager_events
from app.services.task_queue import defer

logger = structlog.get_logger(__name__)

RECOVER_PIPELINE_TASK = "app.tasks.recover_pipeline"
GAP_AGE = timedelta(minutes=10)
INBOX_ALERT_AGE = timedelta(hours=24)
FLAT_MANAGER_LOOKBACK = timedelta(hours=24)
PUBLISHED_REPOS = ("stable", "beta")


async def recover_stale() -> dict[str, int]:
    from app.pipelines.build import START_DEBOUNCED_TASK
    from app.services.job_monitor import (
        BUILD_TIMEOUT_SAFETY_MARGIN,
        DEFAULT_BUILD_TIMEOUT,
        REPROCHECK_TIMEOUT,
        STALE_PIPELINE_TIMEOUT,
    )
    from app.services.publishing import defer_publish

    now = datetime.now(tz=UTC)
    gap_cutoff = now - GAP_AGE
    lookback = now - FLAT_MANAGER_LOOKBACK
    running_cutoff = now - DEFAULT_BUILD_TIMEOUT - BUILD_TIMEOUT_SAFETY_MARGIN
    reprocheck_cutoff = now - REPROCHECK_TIMEOUT - BUILD_TIMEOUT_SAFETY_MARGIN
    workflow_id = Pipeline.params["workflow_id"].as_string()
    settled_at = func.coalesce(Pipeline.finished_at, Pipeline.created_at)
    started_at = func.coalesce(Pipeline.started_at, Pipeline.created_at)
    published_repo = Pipeline.flat_manager_repo.in_(PUBLISHED_REPOS)

    async with get_db() as db:
        expired = cast(
            CursorResult[Any],
            await db.execute(
                update(Pipeline)
                .where(
                    Pipeline.created_at < now - STALE_PIPELINE_TIMEOUT,
                    or_(
                        Pipeline.status == PipelineStatus.PENDING,
                        (Pipeline.status == PipelineStatus.SUCCEEDED)
                        & or_(
                            workflow_id.is_(None),
                            workflow_id != "reprocheck.yml",
                        ),
                        (Pipeline.status == PipelineStatus.PUBLISHING)
                        & Pipeline.update_repo_job_id.isnot(None),
                    ),
                )
                .values(status=PipelineStatus.EXPIRED)
            ),
        )
        if expired.rowcount:
            logger.warning("Expired stale pipelines", count=expired.rowcount)

        successor = aliased(Pipeline)
        debounced = list(
            await db.scalars(
                select(Pipeline.id).where(
                    Pipeline.status == PipelineStatus.PENDING,
                    Pipeline.params["start_after"].as_float() <= gap_cutoff.timestamp(),
                    ~exists().where(
                        successor.app_id == Pipeline.app_id,
                        successor.status == PipelineStatus.RUNNING,
                        successor.created_at > Pipeline.created_at,
                    ),
                )
            )
        )
        for pipeline_id in debounced:
            await defer(
                db,
                START_DEBOUNCED_TASK,
                lock=f"debounce:{pipeline_id}",
                pipeline_id=str(pipeline_id),
            )

        stale = list(
            await db.scalars(
                select(Pipeline.id).where(
                    or_(
                        and_(
                            Pipeline.created_at > lookback,
                            settled_at < gap_cutoff,
                            or_(
                                (Pipeline.status == PipelineStatus.SUCCEEDED)
                                & (
                                    Pipeline.commit_job_id.isnot(None)
                                    | Pipeline.build_id.isnot(None)
                                ),
                                (Pipeline.status == PipelineStatus.COMMITTED)
                                & published_repo
                                & (
                                    Pipeline.publish_job_id.isnot(None)
                                    | Pipeline.build_id.isnot(None)
                                ),
                                (Pipeline.status == PipelineStatus.PUBLISHING)
                                & Pipeline.update_repo_job_id.isnot(None),
                            ),
                        ),
                        (Pipeline.status == PipelineStatus.PUBLISHING)
                        & Pipeline.update_repo_job_id.is_(None)
                        & published_repo,
                        (Pipeline.status == PipelineStatus.RUNNING)
                        & (started_at < running_cutoff),
                        (Pipeline.status == PipelineStatus.RUNNING)
                        & (workflow_id == "reprocheck.yml")
                        & (started_at < reprocheck_cutoff),
                    )
                )
            )
        )
        for pipeline_id in stale:
            await defer(
                db,
                RECOVER_PIPELINE_TASK,
                lock=f"recover:{pipeline_id}",
                pipeline_id=str(pipeline_id),
            )

        unpublished = list(
            await db.scalars(
                select(Pipeline).where(
                    Pipeline.status == PipelineStatus.COMMITTED,
                    published_repo,
                    Pipeline.publish_job_id.is_(None),
                    settled_at < gap_cutoff,
                )
            )
        )
        for pipeline in unpublished:
            await defer_publish(db, pipeline)

        undelivered = list(
            await db.scalars(
                select(FlatManagerWebhookDelivery.id).where(
                    FlatManagerWebhookDelivery.processed_at.is_(None),
                    FlatManagerWebhookDelivery.received_at < gap_cutoff,
                )
            )
        )
        for delivery_id in undelivered:
            await defer(
                db,
                flat_manager_events.TASK_NAME,
                lock=f"flat-manager-event:{delivery_id}",
                delivery_id=delivery_id,
            )
        abandoned = await db.scalar(
            select(func.count()).where(
                FlatManagerWebhookDelivery.processed_at.is_(None),
                FlatManagerWebhookDelivery.received_at < now - INBOX_ALERT_AGE,
            )
        )
        oldest_unprocessed = await db.scalar(
            select(func.min(FlatManagerWebhookDelivery.received_at)).where(
                FlatManagerWebhookDelivery.processed_at.is_(None)
            )
        )

    counts = {
        "expired": expired.rowcount,
        "debounced": len(debounced),
        "stale_pipelines": len(stale),
        "unpublished": len(unpublished),
        "undelivered_events": len(undelivered),
        "abandoned_events": abandoned or 0,
    }
    log = logger.warning if any(counts.values()) else logger.info
    log(
        "Recovery sweep finished",
        oldest_unprocessed_event_at=(
            oldest_unprocessed.isoformat() if oldest_unprocessed else None
        ),
        **counts,
    )
    return counts


async def recover_pipeline(pipeline_id: uuid.UUID) -> bool:
    from app.services.job_monitor import JobMonitor

    async with get_db() as db:
        pipeline = await db.scalar(
            select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update()
        )
        if pipeline is None:
            return False
        initial_status = pipeline.status
        updated = await JobMonitor(db=db).check_and_update_pipeline_jobs(pipeline)
        await flat_manager_events.defer_transition_effects(db, pipeline, initial_status)
    return updated
