from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, func, nulls_last, or_, select
from sqlalchemy.orm import aliased

from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.schemas.pipelines import (
    ReproCheckEntry,
    ReproducibilityData,
    StatusBannerResponse,
)
from app.services.reprocheck_notification import (
    REPROCHECK_BUILD_FAILED,
    REPROCHECK_REPRODUCIBLE,
    REPROCHECK_UNREPRODUCIBLE,
)
from app.status_banner import get_status_banner

dashboard_router = APIRouter(tags=["dashboard"])


async def get_reproducibility_data(
    app_id_filter: str | None = None,
    status_filter: str | None = None,
) -> ReproducibilityData:
    async with get_db(use_replica=True) as db:
        ranked_builds = (
            select(
                Pipeline.id.label("build_id"),
                func.row_number()
                .over(
                    partition_by=Pipeline.app_id,
                    order_by=(
                        nulls_last(Pipeline.finished_at.desc()),
                        Pipeline.created_at.desc(),
                        Pipeline.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(Pipeline.flat_manager_repo == "stable")
            .where(Pipeline.status == PipelineStatus.PUBLISHED)
            .where(
                or_(
                    Pipeline.params["workflow_id"].as_string() != "reprocheck.yml",
                    Pipeline.params["workflow_id"].as_string().is_(None),
                )
            )
            .subquery()
        )
        build_alias = aliased(Pipeline)
        repro_alias = aliased(Pipeline)
        query = (
            select(build_alias, repro_alias)
            .join(
                ranked_builds,
                and_(
                    build_alias.id == ranked_builds.c.build_id,
                    ranked_builds.c.rank == 1,
                ),
            )
            .outerjoin(repro_alias, build_alias.repro_pipeline_id == repro_alias.id)
        )

        if app_id_filter:
            escaped = (
                app_id_filter.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            query = query.where(build_alias.app_id.ilike(f"%{escaped}%", escape="\\"))

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
                repro_result = (
                    repro.params.get("reprocheck_result")
                    if isinstance(repro.params, dict)
                    else None
                )
                if isinstance(repro_result, dict):
                    raw_status = repro_result.get("status_code")
                    raw_result = repro_result.get("result_url")
                    status_code = raw_status if isinstance(raw_status, str) else None
                    result_url = raw_result if isinstance(raw_result, str) else None
                repro_log_url = repro.log_url

            entry = ReproCheckEntry(
                app_id=build.app_id,
                reprocheck_status=status_code,
                result_url=result_url,
                build_commit=build.params.get("sha")
                if isinstance(build.params.get("sha"), str)
                else None,
                git_repo=build.params.get("repo")
                if isinstance(build.params.get("repo"), str)
                else None,
                finished_at=build.finished_at,
                reprocheck_log_url=repro_log_url,
                repro_pipeline_id=repro.id if repro else None,
            )

            if status_filter and (
                (
                    status_filter == "reproducible"
                    and status_code != REPROCHECK_REPRODUCIBLE
                )
                or (
                    status_filter == "unreproducible"
                    and status_code != REPROCHECK_UNREPRODUCIBLE
                )
                or (
                    status_filter == "failed" and status_code != REPROCHECK_BUILD_FAILED
                )
                or status_filter == "none"
                and status_code is not None
            ):
                continue

            if status_code == REPROCHECK_REPRODUCIBLE:
                reproducible.append(entry)
            elif status_code == REPROCHECK_UNREPRODUCIBLE:
                unreproducible.append(entry)
            elif status_code == REPROCHECK_BUILD_FAILED:
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


@dashboard_router.get("/api/reproducible", response_model=ReproducibilityData)
async def reproducible_api(
    app_id: str | None = None,
    status: Literal["reproducible", "unreproducible", "failed", "none"] | None = None,
):
    return await get_reproducibility_data(app_id_filter=app_id, status_filter=status)


@dashboard_router.get("/api/status-banner", response_model=StatusBannerResponse | None)
async def status_banner_api():
    return await get_status_banner()


@dashboard_router.get("/", response_class=RedirectResponse)
async def dashboard(
    app_id: str | None = None,
    target: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    query = urlencode(
        [
            (key, value)
            for key, value in (
                ("appId", app_id),
                ("repo", target),
                ("status", status),
                ("dateFrom", date_from),
                ("dateTo", date_to),
            )
            if value
        ]
    )
    return RedirectResponse(
        f"https://flathub.org/builds{'?' + query if query else ''}", status_code=308
    )


@dashboard_router.get("/status/{app_id}", response_class=RedirectResponse)
async def app_status(app_id: str):
    return RedirectResponse(
        f"https://flathub.org/builds/apps/{quote(app_id, safe='')}", status_code=308
    )


@dashboard_router.get("/reproducible", response_class=RedirectResponse)
async def reproducible_status(
    app_id: str | None = None,
    status: str | None = None,
):
    query = urlencode(
        [
            (key, value)
            for key, value in (("appId", app_id), ("status", status))
            if value
        ]
    )
    return RedirectResponse(
        f"https://flathub.org/builds/reproducible{'?' + query if query else ''}",
        status_code=308,
    )
