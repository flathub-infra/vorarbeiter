from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.config import settings
from app.database import get_db
from app.models.pipeline import Pipeline, PipelineStatus
from app.models.webhook_event import WebhookEvent

BUILDING_LOCALLY_URL = (
    "https://docs.flathub.org/docs/for-app-authors/maintenance#building-locally"
)
PENALTY_EXEMPT_ACTORS = ("flathubbot", "github-actions[bot]")
COUNTED_STATUSES = (
    PipelineStatus.FAILED,
    PipelineStatus.SUCCEEDED,
    PipelineStatus.COMMITTED,
    PipelineStatus.PUBLISHING,
    PipelineStatus.PUBLISHED,
)


def is_penalty_exempt(app_id: str, actor: str | None) -> bool:
    from app.pipelines.build import app_build_types

    return (
        settings.test_build_failure_streak_limit == 0
        or app_id in app_build_types
        or actor in PENALTY_EXEMPT_ACTORS
    )


async def get_penalty_until(
    repo: str, pr_number: int | str, now: datetime | None = None
) -> datetime | None:
    limit = settings.test_build_failure_streak_limit
    if limit <= 0:
        return None

    conditions = []
    if repo != "flathub/flathub":
        conditions.append(Pipeline.app_id == repo.split("/")[-1])

    query = (
        select(Pipeline.status, Pipeline.finished_at)
        .outerjoin(WebhookEvent, Pipeline.webhook_event_id == WebhookEvent.id)
        .where(
            *conditions,
            Pipeline.flat_manager_repo == "test",
            Pipeline.params["repo"].as_string() == repo,
            Pipeline.params["pr_number"].as_string() == str(pr_number),
            Pipeline.status.in_(COUNTED_STATUSES),
            or_(
                WebhookEvent.actor.is_(None),
                WebhookEvent.actor.not_in(PENALTY_EXEMPT_ACTORS),
            ),
        )
        .order_by(Pipeline.created_at.desc())
        .limit(limit)
    )
    async with get_db(use_replica=False) as db:
        rows = (await db.execute(query)).all()

    if len(rows) != limit or any(
        status != PipelineStatus.FAILED or finished_at is None
        for status, finished_at in rows
    ):
        return None

    latest = max(
        finished_at.replace(tzinfo=UTC) if finished_at.tzinfo is None else finished_at
        for _, finished_at in rows
    )
    until = latest + timedelta(minutes=settings.test_build_penalty_minutes)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return until if current < until else None


def format_penalty_notice(until: datetime) -> str:
    return (
        f"The last {settings.test_build_failure_streak_limit} test builds failed. "
        f"Test builds are paused until {until:%H:%M} UTC. "
        f"Please [build and test locally]({BUILDING_LOCALLY_URL}). "
        "After that time, push again or comment `bot, build` to start a new build."
    )
