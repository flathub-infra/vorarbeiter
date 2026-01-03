from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, case, cast, func, or_, select

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus

dashboard_router = APIRouter(tags=["dashboard"])

templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def format_time(dt: datetime | None) -> str:
    if dt is None:
        return "-"

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    diff = now - dt
    seconds = diff.total_seconds()

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    else:
        return dt.strftime("%Y-%m-%d %H:%M")


def format_duration(started_at: datetime | None, finished_at: datetime | None) -> str:
    if started_at is None:
        return "-"
    if finished_at is None:
        return "In progress"

    diff = finished_at - started_at
    seconds = int(diff.total_seconds())

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


templates.env.globals["format_time"] = format_time
templates.env.globals["format_duration"] = format_duration


async def get_recent_pipelines(
    status: str | None = None,
    app_id: str | None = None,
    target: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[Pipeline]:
    limit = 25 if app_id and not (date_from or date_to) else 50

    async with get_db(use_replica=True) as db:
        query = (
            select(Pipeline)
            .where(
                or_(
                    cast(Pipeline.params["workflow_id"], String) != '"reprocheck.yml"',
                    Pipeline.params["workflow_id"].is_(None),
                )
            )
            .where(Pipeline.app_id != "flathub")
            .order_by(
                case(
                    (
                        Pipeline.status.in_(
                            [PipelineStatus.PENDING, PipelineStatus.RUNNING]
                        ),
                        0,
                    ),
                    else_=1,
                ),
                func.coalesce(Pipeline.finished_at, Pipeline.created_at).desc(),
            )
            .limit(limit)
        )

        if status:
            try:
                pipeline_status = PipelineStatus(status)
                query = query.where(Pipeline.status == pipeline_status)
            except ValueError:
                pass

        if app_id:
            query = query.where(Pipeline.app_id.ilike(f"%{app_id}%"))

        if target:
            query = query.where(Pipeline.flat_manager_repo == target)

        if date_from:
            try:
                from_date = datetime.strptime(date_from, "%Y-%m-%dT%H:%M")
                query = query.where(Pipeline.started_at >= from_date)
            except ValueError:
                pass

        if date_to:
            try:
                to_date = datetime.strptime(date_to, "%Y-%m-%dT%H:%M")
                query = query.where(Pipeline.started_at <= to_date)
            except ValueError:
                pass

        result = await db.execute(query)
        return list(result.scalars().all())


@dashboard_router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    status: str | None = None,
    app_id: str | None = None,
    target: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    pipelines = await get_recent_pipelines(
        status=status,
        app_id=app_id,
        target=target,
        date_from=date_from,
        date_to=date_to,
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "pipelines": pipelines,
            "statuses": [
                PipelineStatus.RUNNING,
                PipelineStatus.SUCCEEDED,
                PipelineStatus.CANCELLED,
                PipelineStatus.COMMITTED,
                PipelineStatus.FAILED,
                PipelineStatus.PUBLISHED,
                PipelineStatus.PUBLISHING,
            ],
            "filters": {
                "status": status or "",
                "app_id": app_id or "",
                "target": target or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
            "flat_manager_url": settings.flat_manager_url,
        },
    )


@dashboard_router.get("/api/htmx/builds", response_class=HTMLResponse)
async def builds_table(
    request: Request,
    status: str | None = None,
    app_id: str | None = None,
    target: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    pipelines = await get_recent_pipelines(
        status=status,
        app_id=app_id,
        target=target,
        date_from=date_from,
        date_to=date_to,
    )

    return templates.TemplateResponse(
        request=request,
        name="partials/builds.html",
        context={
            "pipelines": pipelines,
            "flat_manager_url": settings.flat_manager_url,
        },
    )
