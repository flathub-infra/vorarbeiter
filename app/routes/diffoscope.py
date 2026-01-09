import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import Response

from app.database import get_db
from app.models import Pipeline
from app.services.diffoscope import fetch_diffoscope_report, rewrite_asset_paths

diffoscope_router = APIRouter(prefix="/diffoscope", tags=["diffoscope"])


async def _get_result_url(pipeline_id: uuid.UUID) -> str:
    async with get_db(use_replica=True) as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Pipeline not found",
            )

        reprocheck_result = pipeline.params.get("reprocheck_result", {})
        result_url = reprocheck_result.get("result_url")

        if not result_url:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="No diffoscope report available for this pipeline",
            )

        return result_url


@diffoscope_router.get("/{pipeline_id}")
async def view_diffoscope(request: Request, pipeline_id: uuid.UUID) -> Response:
    result_url = await _get_result_url(pipeline_id)
    report = await fetch_diffoscope_report(result_url)

    if not report or not report.html:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch diffoscope report",
        )

    base_path = f"/diffoscope/{pipeline_id}"
    html = rewrite_asset_paths(report.html, base_path)

    return Response(content=html, media_type="text/html")


@diffoscope_router.get("/{pipeline_id}/common.css")
async def diffoscope_css(pipeline_id: uuid.UUID) -> Response:
    result_url = await _get_result_url(pipeline_id)
    report = await fetch_diffoscope_report(result_url)

    if not report or not report.css:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)

    return Response(content=report.css, media_type="text/css")


@diffoscope_router.get("/{pipeline_id}/icon.png")
async def diffoscope_icon(pipeline_id: uuid.UUID) -> Response:
    result_url = await _get_result_url(pipeline_id)
    report = await fetch_diffoscope_report(result_url)

    if not report or not report.icon:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)

    return Response(content=report.icon, media_type="image/png")
