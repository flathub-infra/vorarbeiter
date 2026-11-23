import base64
import functools
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import FlatManagerWebhookDelivery, Pipeline, PipelineStatus
from app.services.task_queue import defer
from app.utils.flat_manager import JobKind, JobStatus, PublishedState, RepoState

logger = structlog.get_logger(__name__)

TASK_NAME = "app.tasks.flat_manager_event"
QUEUE = "flat-manager"
JOB_FINISHED = "job.finished"
ORPHAN_AFTER = timedelta(minutes=15)

JOB_PIPELINE_STATES = {
    JobKind.COMMIT: ("commit_job_id", PipelineStatus.SUCCEEDED),
    JobKind.PUBLISH: ("publish_job_id", PipelineStatus.COMMITTED),
    JobKind.UPDATE_REPO: ("update_repo_job_id", PipelineStatus.PUBLISHING),
}


class PipelineNotReady(Exception):
    pass


class FlatManagerJob(BaseModel):
    id: int
    kind: JobKind
    status: JobStatus
    repo: str | None
    results: str | None
    start_after: datetime | None


class FlatManagerBuild(BaseModel):
    id: int
    repo: str
    app_id: str | None
    repo_state: RepoState
    repo_state_reason: str | None
    published_state: PublishedState
    published_state_reason: str | None
    commit_job_id: int | None
    publish_job_id: int | None


class JobFinished(BaseModel):
    job: FlatManagerJob
    build: FlatManagerBuild | None


class FlatManagerEvent(BaseModel):
    id: int
    type: str
    created_at: AwareDatetime
    data: dict[str, Any]


@functools.cache
def _read_key(path: str) -> bytes:
    return base64.b64decode(Path(path).read_text().strip(), validate=True)


def webhook_key() -> bytes | None:
    path = settings.flat_manager_webhook_secret_file
    return _read_key(path) if path else None


async def record_event(event: FlatManagerEvent) -> bool:
    async with get_db() as db:
        try:
            async with db.begin_nested():
                db.add(
                    FlatManagerWebhookDelivery(
                        id=event.id,
                        event_type=event.type,
                        payload=event.model_dump(mode="json"),
                        received_at=datetime.now(tz=UTC),
                    )
                )
        except IntegrityError:
            logger.info("Duplicate flat-manager delivery", delivery_id=event.id)
            return False
        await defer(db, TASK_NAME, delivery_id=event.id)
    return True


async def defer_transition_effects(
    db: AsyncSession, pipeline: Pipeline, initial_status: PipelineStatus
) -> None:
    from app.pipelines.build import defer_start_pending
    from app.services.github_notifier import NOTIFY_COMMITTED_TASK
    from app.services.permission_assessment import SUBMIT_ASSESSMENT_TASK
    from app.services.publishing import defer_publish

    if pipeline.status == initial_status:
        return
    await defer_start_pending(db)
    if pipeline.status in (PipelineStatus.COMMITTED, PipelineStatus.PUBLISHED):
        await defer_publish(db, pipeline)
    if pipeline.status != PipelineStatus.COMMITTED:
        return
    await defer(
        db,
        SUBMIT_ASSESSMENT_TASK,
        lock=f"assessment:{pipeline.id}",
        pipeline_id=str(pipeline.id),
    )
    await defer(
        db,
        NOTIFY_COMMITTED_TASK,
        lock=f"notify-committed:{pipeline.id}",
        pipeline_id=str(pipeline.id),
    )


async def _matching_pipelines(db: AsyncSession, event: JobFinished) -> list[Pipeline]:
    job, build = event.job, event.build
    if job.kind == JobKind.CHECK:
        if build is None:
            return []
        condition = Pipeline.build_id == build.id
    else:
        column = getattr(Pipeline, JOB_PIPELINE_STATES[job.kind][0])
        condition = column == job.id
        if build is not None:
            condition = or_(
                condition, (Pipeline.build_id == build.id) & column.is_(None)
            )
    result = await db.execute(
        select(Pipeline).where(condition).order_by(Pipeline.id).with_for_update()
    )
    return list(result.scalars())


async def _apply(db: AsyncSession, event: JobFinished, pipeline: Pipeline) -> None:
    from app.services.job_monitor import JobMonitor
    from app.services.publishing import defer_publish

    job = event.job
    if job.kind == JobKind.CHECK:
        logger.info(
            "Flat-manager check job finished",
            pipeline_id=str(pipeline.id),
            job_id=job.id,
            build_id=pipeline.build_id,
        )
        if pipeline.status == PipelineStatus.COMMITTED:
            await defer_publish(db, pipeline)
        return

    column, expected_status = JOB_PIPELINE_STATES[job.kind]
    if getattr(pipeline, column) is None:
        setattr(pipeline, column, job.id)
        logger.info(
            "Stored job id from flat-manager event",
            pipeline_id=str(pipeline.id),
            **{column: job.id},
        )
    if pipeline.status != expected_status:
        logger.info(
            "Ignoring flat-manager event for pipeline in another state",
            pipeline_id=str(pipeline.id),
            job_id=job.id,
            job_kind=job.kind.name,
            pipeline_status=pipeline.status.value,
        )
        return

    initial_status = pipeline.status
    await JobMonitor(db=db).check_and_update_pipeline_jobs(pipeline, event)
    await defer_transition_effects(db, pipeline, initial_status)


async def process_event(delivery_id: int) -> None:
    async with get_db() as db:
        claimed = await db.execute(
            update(FlatManagerWebhookDelivery)
            .where(
                FlatManagerWebhookDelivery.id == delivery_id,
                FlatManagerWebhookDelivery.processed_at.is_(None),
            )
            .values(processed_at=datetime.now(tz=UTC))
            .returning(FlatManagerWebhookDelivery.payload)
        )
        payload = claimed.scalar_one_or_none()
        if payload is None:
            return
        envelope = FlatManagerEvent.model_validate(payload)
        event = JobFinished.model_validate(envelope.data)
        log = logger.bind(
            delivery_id=delivery_id,
            event_type=envelope.type,
            job_id=event.job.id,
            job_kind=event.job.kind.name,
            build_id=event.build.id if event.build else None,
        )
        if event.job.kind not in (*JOB_PIPELINE_STATES, JobKind.CHECK):
            log.info("Flat-manager event needs no pipeline")
            return

        pipelines = await _matching_pipelines(db, event)
        if not pipelines:
            if datetime.now(tz=UTC) - envelope.created_at < ORPHAN_AFTER:
                raise PipelineNotReady(delivery_id)
            log.warning("Flat-manager event orphaned, no matching pipeline")
            return

        log.info("Processing flat-manager event", pipelines=len(pipelines))
        for pipeline in pipelines:
            await _apply(db, event, pipeline)
