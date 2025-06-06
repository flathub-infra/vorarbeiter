import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines import BuildPipeline
from app.schemas.pipelines import (
    PipelineResponse,
    PipelineSummary,
    PipelineTriggerRequest,
    PublishSummary,
)
from app.services import pipeline_service, publishing_service
from app.services.job_monitor import JobMonitor

logger = structlog.get_logger(__name__)
pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])
security = HTTPBearer()
api_security = HTTPBearer()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(api_security),
):
    if credentials.credentials != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
    return credentials.credentials


@pipelines_router.post(
    "/pipelines",
    response_model=dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def trigger_pipeline(
    data: PipelineTriggerRequest,
    token: str = Depends(verify_token),
):
    try:
        result = await pipeline_service.trigger_manual_pipeline(
            app_id=data.app_id,
            params=data.params,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@pipelines_router.get(
    "/pipelines",
    response_model=list[PipelineSummary],
    status_code=status.HTTP_200_OK,
)
async def list_pipelines(
    app_id: str | None = None,
    status_filter: PipelineStatus | None = None,
    triggered_by: PipelineTrigger | None = None,
    target_repo: str | None = None,
    limit: int | None = 10,
):
    if status_filter:
        try:
            status_filter = pipeline_service.validate_status_filter(status_filter)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    if triggered_by:
        try:
            triggered_by = pipeline_service.validate_trigger_filter(triggered_by)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    async with get_db() as db:
        pipelines = await pipeline_service.list_pipelines_with_filters(
            db=db,
            app_id=app_id,
            status_filter=status_filter,
            triggered_by=triggered_by,
            target_repo=target_repo,
            limit=limit or 10,
        )

        return [
            pipeline_service.pipeline_to_summary(pipeline) for pipeline in pipelines
        ]


@pipelines_router.get(
    "/pipelines/{pipeline_id}",
    response_model=PipelineResponse,
    status_code=status.HTTP_200_OK,
)
async def get_pipeline(
    pipeline_id: uuid.UUID,
):
    async with get_db() as db:
        pipeline = await pipeline_service.get_pipeline_with_job_updates(db, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        return pipeline_service.pipeline_to_response(pipeline)


@pipelines_router.post(
    "/pipelines/{pipeline_id}/callback",
    status_code=status.HTTP_200_OK,
)
async def pipeline_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    build_pipeline = BuildPipeline()

    try:
        await build_pipeline.verify_callback_token(
            pipeline_id=pipeline_id,
            token=credentials.credentials,
        )
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid callback token",
            )

    try:
        updated_pipeline, updates = await build_pipeline.handle_callback(
            pipeline_id=pipeline_id,
            callback_data=data,
        )
    except ValueError as e:
        if "Log URL already set" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Log URL already set",
            )
        elif "Pipeline status already finalized" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pipeline status already finalized",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    return {
        "status": "ok",
        "pipeline_id": str(pipeline_id),
        **updates,
    }


@pipelines_router.get(
    "/pipelines/{pipeline_id}/log_url",
)
async def redirect_to_log_url(
    pipeline_id: uuid.UUID,
    response: Response,
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if not pipeline.log_url:
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = "10"
            return {"detail": "Log URL not available yet"}

        response.status_code = status.HTTP_307_TEMPORARY_REDIRECT
        response.headers["Location"] = pipeline.log_url
        return {}


@pipelines_router.post(
    "/pipelines/publish",
    response_model=PublishSummary,
    status_code=status.HTTP_200_OK,
)
async def publish_pipelines(
    token: str = Depends(verify_token),
):
    async with get_db() as db:
        result = await publishing_service.publish_pipelines(db)
        return PublishSummary(
            published=result.published,
            superseded=result.superseded,
            errors=result.errors,
        )


@pipelines_router.post(
    "/pipelines/check-jobs",
    status_code=status.HTTP_200_OK,
)
async def check_pipeline_jobs(
    token: str = Depends(verify_token),
):
    """Check job statuses for all SUCCEEDED pipelines and update accordingly."""
    async with get_db() as db:
        from sqlalchemy import select

        query = select(Pipeline).where(
            Pipeline.status == PipelineStatus.SUCCEEDED,
            Pipeline.commit_job_id.isnot(None),
        )
        result = await db.execute(query)
        succeeded_pipelines = list(result.scalars().all())

        job_monitor = JobMonitor()
        updated_count = 0

        for pipeline in succeeded_pipelines:
            if await job_monitor.check_and_update_pipeline_jobs(db, pipeline):
                updated_count += 1

        if updated_count > 0:
            await db.commit()

        return {
            "status": "completed",
            "checked_pipelines": len(succeeded_pipelines),
            "updated_pipelines": updated_count,
        }
