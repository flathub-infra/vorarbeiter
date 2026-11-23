import base64
import functools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import get_db
from app.models import FlatManagerWebhookDelivery
from app.services.task_queue import defer
from app.utils.flat_manager import JobKind, JobStatus, PublishedState, RepoState

logger = structlog.get_logger(__name__)

TASK_NAME = "app.tasks.flat_manager_event"
QUEUE = "flat-manager"
JOB_FINISHED = "job.finished"


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
    event = JobFinished.model_validate(payload["data"])
    logger.info(
        "Flat-manager event received",
        delivery_id=delivery_id,
        event_type=payload["type"],
        job_id=event.job.id,
        build_id=event.build.id if event.build else None,
    )
