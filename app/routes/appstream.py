import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.services.appstream import PreviewError, PreviewUnavailable, fetch_preview

logger = structlog.get_logger(__name__)
appstream_router = APIRouter(prefix="/appstream", tags=["appstream"])
_templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=select_autoescape(),
)
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'none'; "
        "style-src 'unsafe-inline' https://rsms.me; font-src https://rsms.me; "
        "img-src https:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}


def _error(status: int, message: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!DOCTYPE html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>AppStream preview unavailable</title><h1>AppStream preview unavailable</h1><p>{escape(message)}</p></html>",
        status_code=status,
        headers=_HEADERS,
    )


@appstream_router.get("/{pipeline_id}", response_class=HTMLResponse)
async def view_appstream(pipeline_id: uuid.UUID) -> HTMLResponse:
    async with get_db(use_replica=True) as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline or not pipeline.build_id:
            return _error(404, "No test build exists for this pipeline.")
        if pipeline.status in {
            PipelineStatus.PENDING,
            PipelineStatus.RUNNING,
            PipelineStatus.SUCCEEDED,
        }:
            return _error(409, "The test build is not committed yet.")
        if pipeline.status != PipelineStatus.COMMITTED:
            return _error(
                404, "No committed test build is available for this pipeline."
            )
        build_id = pipeline.build_id
        app_id = pipeline.app_id
        params = pipeline.params or {}
        log_url = pipeline.log_url

    try:
        context = await fetch_preview(build_id, app_id)
    except PreviewUnavailable:
        return _error(
            404,
            "The build metadata is unavailable. The test build may no longer exist.",
        )
    except PreviewError:
        logger.exception(
            "Failed to fetch AppStream preview", pipeline_id=str(pipeline_id)
        )
        return _error(502, "The build metadata could not be loaded. Try again later.")

    repo = params.get("repo")
    pr_number = params.get("pr_number")
    pr_url = (
        f"https://github.com/{repo}/pull/{pr_number}" if repo and pr_number else None
    )
    content = _templates.get_template("appstream.html").render(
        **context,
        build_id=build_id,
        pr_url=pr_url,
        sha=params.get("verified_sha") or params.get("sha"),
        log_url=log_url,
    )
    return HTMLResponse(content, headers=_HEADERS)
