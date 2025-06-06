import secrets
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.future import select

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines import BuildPipeline, CallbackData
from app.schemas.pipelines import (
    PipelineLogUrlCallback,
    PipelineResponse,
    PipelineStatusCallback,
    PipelineSummary,
    PipelineTriggerRequest,
    PublishSummary,
)
from app.services import publishing_service
from app.utils.flat_manager import FlatManagerClient

logger = structlog.get_logger(__name__)
pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])
security = HTTPBearer()
api_security = HTTPBearer()


async def update_pipeline_job_ids(pipeline: Pipeline) -> bool:
    if pipeline.commit_job_id is not None and pipeline.publish_job_id is not None:
        return False

    if not pipeline.build_id:
        return False

    try:
        flat_manager = FlatManagerClient(
            url=settings.flat_manager_url,
            token=settings.flat_manager_token,
        )
        build_info = await flat_manager.get_build_info(pipeline.build_id)
        build_data = build_info.get("build", {})

        commit_job_id = build_data.get("commit_job_id")
        publish_job_id = build_data.get("publish_job_id")

        updated = False
        if commit_job_id is not None and pipeline.commit_job_id is None:
            pipeline.commit_job_id = commit_job_id
            updated = True

        if publish_job_id is not None and pipeline.publish_job_id is None:
            pipeline.publish_job_id = publish_job_id
            updated = True

        return updated
    except Exception as e:
        logger.error(
            "Failed to fetch job IDs from flat-manager",
            pipeline_id=str(pipeline.id),
            build_id=pipeline.build_id,
            error=str(e),
        )
        return False


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
    pipeline_service = BuildPipeline()

    pipeline = await pipeline_service.create_pipeline(
        app_id=data.app_id,
        params=data.params,
        webhook_event_id=None,
    )

    async with get_db() as db:
        db_pipeline = await db.get(Pipeline, pipeline.id)
        if db_pipeline is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline.id} not found",
            )
        db_pipeline.triggered_by = PipelineTrigger.MANUAL
        await db.flush()
        pipeline = db_pipeline

    pipeline = await pipeline_service.start_pipeline(
        pipeline_id=pipeline.id,
    )

    return {
        "status": "created",
        "pipeline_id": str(pipeline.id),
        "app_id": pipeline.app_id,
        "pipeline_status": pipeline.status.value,
    }


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
    limit = min(max(1, limit or 10), 100)

    async with get_db() as db:
        stmt = select(Pipeline).order_by(Pipeline.created_at.desc())

        if app_id is not None:
            stmt = stmt.where(Pipeline.app_id.startswith(app_id))

        if status_filter is not None:
            try:
                pipeline_status = PipelineStatus(status_filter)
                stmt = stmt.where(Pipeline.status == pipeline_status)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status value: {status_filter}. Valid values are: {[s.value for s in PipelineStatus]}",
                )

        if triggered_by is not None:
            try:
                pipeline_trigger = PipelineTrigger(triggered_by)
                stmt = stmt.where(Pipeline.triggered_by == pipeline_trigger)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid triggered_by value: {triggered_by}. Valid values are: {[t.value for t in PipelineTrigger]}",
                )

        if target_repo is not None:
            stmt = stmt.where(Pipeline.flat_manager_repo == target_repo)

        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        pipelines = list(result.scalars().all())

        updated_job_ids = False
        for pipeline in pipelines:
            if (
                pipeline.commit_job_id is None or pipeline.publish_job_id is None
            ) and pipeline.build_id:
                if await update_pipeline_job_ids(pipeline):
                    updated_job_ids = True

        if updated_job_ids:
            await db.commit()

        return [
            PipelineSummary(
                id=str(pipeline.id),
                app_id=pipeline.app_id,
                status=pipeline.status,
                repo=str(pipeline.flat_manager_repo)
                if pipeline.flat_manager_repo is not None
                else None,
                triggered_by=pipeline.triggered_by,
                build_id=pipeline.build_id,
                commit_job_id=pipeline.commit_job_id,
                publish_job_id=pipeline.publish_job_id,
                created_at=pipeline.created_at,
                started_at=pipeline.started_at,
                finished_at=pipeline.finished_at,
                published_at=pipeline.published_at,
            )
            for pipeline in pipelines
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
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if (
            pipeline.commit_job_id is None or pipeline.publish_job_id is None
        ) and pipeline.build_id:
            if await update_pipeline_job_ids(pipeline):
                await db.commit()

        return PipelineResponse(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by,
            log_url=pipeline.log_url,
            build_id=pipeline.build_id,
            commit_job_id=pipeline.commit_job_id,
            publish_job_id=pipeline.publish_job_id,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
        )


@pipelines_router.post(
    "/pipelines/{pipeline_id}/callback",
    status_code=status.HTTP_200_OK,
)
async def pipeline_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if not secrets.compare_digest(credentials.credentials, pipeline.callback_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid callback token",
            )

    if "status" in data:
        try:
            PipelineStatusCallback(**data)
        except Exception:
            raise
    elif "log_url" in data:
        try:
            PipelineLogUrlCallback(**data)
        except Exception:
            raise
    elif not any(
        field in data
        for field in ["app_id", "is_extra_data", "end_of_life", "end_of_life_rebase"]
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request must contain either 'status', 'log_url', 'app_id', 'is_extra_data', 'end_of_life', or 'end_of_life_rebase' field",
        )

    callback_data = CallbackData(
        status=data.get("status"),
        log_url=data.get("log_url"),
        app_id=data.get("app_id"),
        is_extra_data=data.get("is_extra_data"),
        end_of_life=data.get("end_of_life"),
        end_of_life_rebase=data.get("end_of_life_rebase"),
    )

    pipeline_service = BuildPipeline()
    try:
        updated_pipeline, updates = await pipeline_service.handle_callback(
            pipeline_id=pipeline_id,
            callback_data=callback_data,
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
