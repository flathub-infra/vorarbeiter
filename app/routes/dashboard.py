import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.orm import aliased

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


def format_cost(cost: float | None) -> str:
    if cost is None:
        return "-"
    return f"${cost:.2f}"


templates.env.globals["format_time"] = format_time
templates.env.globals["format_duration"] = format_duration
templates.env.globals["format_cost"] = format_cost
templates.env.globals["flat_manager_url"] = settings.flat_manager_url


def group_pipelines(
    pipelines: list[Pipeline],
) -> dict[str, list[Pipeline]]:
    awaiting_publishing: list[Pipeline] = []
    in_progress: list[Pipeline] = []
    completed: list[Pipeline] = []

    in_progress_statuses = {
        PipelineStatus.PENDING,
        PipelineStatus.RUNNING,
        PipelineStatus.SUCCEEDED,
        PipelineStatus.PUBLISHING,
    }
    awaiting_statuses = {PipelineStatus.COMMITTED}

    for p in pipelines:
        if p.status in awaiting_statuses and p.flat_manager_repo in ("stable", "beta"):
            awaiting_publishing.append(p)
        elif p.status in in_progress_statuses:
            in_progress.append(p)
        else:
            completed.append(p)

    return {
        "awaiting_publishing": awaiting_publishing,
        "in_progress": in_progress,
        "completed": completed,
    }


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


class ReprocheckInfo:
    __slots__ = ("status_code", "result_url")

    def __init__(self, status_code: str | None, result_url: str | None):
        self.status_code = status_code
        self.result_url = result_url


async def get_app_builds(
    app_id: str, target_repo: str, limit: int = 5
) -> tuple[list[Pipeline], dict[uuid.UUID, ReprocheckInfo]]:
    async with get_db(use_replica=True) as db:
        query = (
            select(Pipeline)
            .where(Pipeline.app_id == app_id)
            .where(Pipeline.flat_manager_repo == target_repo)
            .where(
                or_(
                    cast(Pipeline.params["workflow_id"], String) != '"reprocheck.yml"',
                    Pipeline.params["workflow_id"].is_(None),
                )
            )
            .order_by(Pipeline.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(query)
        pipelines = list(result.scalars().all())

        repro_ids = [p.repro_pipeline_id for p in pipelines if p.repro_pipeline_id]
        reprocheck_status: dict[uuid.UUID, ReprocheckInfo] = {}

        if repro_ids:
            repro_query = select(Pipeline).where(Pipeline.id.in_(repro_ids))
            repro_result = await db.execute(repro_query)
            for repro_pipeline in repro_result.scalars().all():
                result_data = repro_pipeline.params.get("reprocheck_result", {})
                status_code = result_data.get("status_code")
                result_url = result_data.get("result_url")
                for p in pipelines:
                    if p.repro_pipeline_id == repro_pipeline.id:
                        reprocheck_status[p.id] = ReprocheckInfo(
                            status_code, result_url
                        )

        return pipelines, reprocheck_status


@dataclass
class ReproCheckEntry:
    app_id: str
    reprocheck_status: str | None
    result_url: str | None
    build_commit: str | None
    git_repo: str | None
    finished_at: datetime | None
    reprocheck_log_url: str | None


@dataclass
class ReproducibilityData:
    reproducible: list[ReproCheckEntry]
    unreproducible: list[ReproCheckEntry]
    failed_to_rebuild: list[ReproCheckEntry]
    unknown: list[ReproCheckEntry]


async def get_reproducibility_data(
    app_id_filter: str | None = None,
    status_filter: str | None = None,
) -> ReproducibilityData:
    async with get_db(use_replica=True) as db:
        latest_build_subq = (
            select(
                Pipeline.app_id,
                func.max(Pipeline.finished_at).label("max_finished"),
            )
            .where(Pipeline.flat_manager_repo == "stable")
            .where(Pipeline.status == PipelineStatus.PUBLISHED)
            .where(
                or_(
                    cast(Pipeline.params["workflow_id"], String) != '"reprocheck.yml"',
                    Pipeline.params["workflow_id"].is_(None),
                )
            )
            .group_by(Pipeline.app_id)
            .subquery()
        )

        build_alias = aliased(Pipeline)
        repro_alias = aliased(Pipeline)

        query = (
            select(build_alias, repro_alias)
            .join(
                latest_build_subq,
                and_(
                    build_alias.app_id == latest_build_subq.c.app_id,
                    build_alias.finished_at == latest_build_subq.c.max_finished,
                ),
            )
            .outerjoin(repro_alias, build_alias.repro_pipeline_id == repro_alias.id)
            .where(build_alias.flat_manager_repo == "stable")
            .where(build_alias.status == PipelineStatus.PUBLISHED)
        )

        if app_id_filter:
            query = query.where(build_alias.app_id.ilike(f"%{app_id_filter}%"))

        result = await db.execute(query)
        rows = result.all()

        reproducible: list[ReproCheckEntry] = []
        unreproducible: list[ReproCheckEntry] = []
        failed_to_rebuild: list[ReproCheckEntry] = []
        unknown: list[ReproCheckEntry] = []

        for build, repro in rows:
            status_code = None
            result_url = None
            repro_log_url = None

            if repro:
                repro_result = repro.params.get("reprocheck_result", {})
                status_code = repro_result.get("status_code")
                result_url = repro_result.get("result_url")
                repro_log_url = repro.log_url

            entry = ReproCheckEntry(
                app_id=build.app_id,
                reprocheck_status=status_code,
                result_url=result_url,
                build_commit=build.params.get("sha"),
                git_repo=build.params.get("repo"),
                finished_at=build.finished_at,
                reprocheck_log_url=repro_log_url,
            )

            if status_filter:
                if status_filter == "reproducible" and status_code != "0":
                    continue
                elif status_filter == "unreproducible" and status_code != "42":
                    continue
                elif status_filter == "failed" and status_code != "1":
                    continue
                elif status_filter == "none" and status_code is not None:
                    continue

            if status_code == "0":
                reproducible.append(entry)
            elif status_code == "42":
                unreproducible.append(entry)
            elif status_code == "1":
                failed_to_rebuild.append(entry)
            else:
                unknown.append(entry)

        reproducible.sort(key=lambda x: x.app_id)
        unreproducible.sort(key=lambda x: x.app_id)
        failed_to_rebuild.sort(key=lambda x: x.app_id)
        unknown.sort(key=lambda x: x.app_id)

        return ReproducibilityData(
            reproducible=reproducible,
            unreproducible=unreproducible,
            failed_to_rebuild=failed_to_rebuild,
            unknown=unknown,
        )


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

    grouped = group_pipelines(pipelines)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "grouped_pipelines": grouped,
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

    grouped = group_pipelines(pipelines)

    return templates.TemplateResponse(
        request=request,
        name="partials/builds.html",
        context={
            "grouped_pipelines": grouped,
            "flat_manager_url": settings.flat_manager_url,
        },
    )


@dashboard_router.get("/status/{app_id}", response_class=HTMLResponse)
async def app_status(request: Request, app_id: str):
    stable_builds_all, stable_reprocheck = await get_app_builds(
        app_id, "stable", limit=10
    )
    beta_builds, _ = await get_app_builds(app_id, "beta", limit=5)
    test_builds, _ = await get_app_builds(app_id, "test", limit=5)

    chart_data = [
        {
            "date": p.started_at.strftime("%Y-%m-%d %H:%M"),
            "duration": round((p.finished_at - p.started_at).total_seconds() / 60, 1),
        }
        for p in reversed(stable_builds_all)
        if p.started_at and p.finished_at
    ]

    return templates.TemplateResponse(
        request=request,
        name="app_status.html",
        context={
            "app_id": app_id,
            "stable_builds": stable_builds_all[:5],
            "beta_builds": beta_builds,
            "test_builds": test_builds,
            "stable_reprocheck": stable_reprocheck,
            "chart_data": chart_data,
            "flat_manager_url": settings.flat_manager_url,
        },
    )


@dashboard_router.get("/reproducible", response_class=HTMLResponse)
async def reproducible_status(
    request: Request,
    app_id: str | None = None,
    status: str | None = None,
):
    data = await get_reproducibility_data(
        app_id_filter=app_id,
        status_filter=status,
    )

    return templates.TemplateResponse(
        request=request,
        name="reproducible.html",
        context={
            "data": data,
            "filters": {
                "app_id": app_id or "",
                "status": status or "",
            },
            "expand_all": bool(app_id),
        },
    )


@dashboard_router.get("/api/htmx/reproducible", response_class=HTMLResponse)
async def reproducible_table(
    request: Request,
    app_id: str | None = None,
    status: str | None = None,
):
    data = await get_reproducibility_data(
        app_id_filter=app_id,
        status_filter=status,
    )

    return templates.TemplateResponse(
        request=request,
        name="partials/reproducible_tables.html",
        context={
            "data": data,
            "expand_all": bool(app_id),
        },
    )
