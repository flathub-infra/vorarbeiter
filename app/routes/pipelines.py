import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi import status as http_status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines import BuildPipeline
from app.schemas.pipelines import (
    PipelineResponse,
    PipelineSummary,
    PipelineTriggerRequest,
    PipelineType,
    ReprocheckStatus,
)
from app.services import pipeline_service

pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])
security = HTTPBearer()


async def get_verified_build_pipeline(
    pipeline_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> BuildPipeline:
    build_pipeline = BuildPipeline()

    try:
        await build_pipeline.verify_callback_token(
            pipeline_id=pipeline_id,
            token=credentials.credentials,
        )
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Invalid callback token",
        )

    return build_pipeline


VerifiedBuildPipeline = Annotated[BuildPipeline, Depends(get_verified_build_pipeline)]


async def execute_callback_handler(
    handler: Callable[..., Awaitable[tuple[Any, dict[str, Any]]]],
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    conflict_messages: list[str] | None = None,
) -> dict[str, Any]:
    try:
        _, updates = await handler(pipeline_id=pipeline_id, callback_data=data)
    except ValueError as e:
        error_str = str(e)
        if conflict_messages:
            for msg in conflict_messages:
                if msg in error_str:
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail=msg,
                    )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=error_str,
        )

    return {
        "status": "ok",
        "pipeline_id": str(pipeline_id),
        **updates,
    }


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    if credentials.credentials != settings.admin_token:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
    return credentials.credentials


@pipelines_router.post(
    "/pipelines",
    response_model=dict[str, Any],
    status_code=http_status.HTTP_201_CREATED,
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
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        raise


@pipelines_router.get(
    "/pipelines",
    response_model=list[PipelineSummary],
    status_code=http_status.HTTP_200_OK,
)
async def list_pipelines(
    app_id: str | None = None,
    type: PipelineType = PipelineType.BUILD,
    status: PipelineStatus | str | None = None,
    triggered_by: PipelineTrigger | None = None,
    target_repo: str | None = None,
    limit: int | None = 10,
):
    reprocheck_status: ReprocheckStatus | None = None
    pipeline_status: PipelineStatus | None = None

    if status:
        if type == PipelineType.REPROCHECK and status in [
            s.value for s in ReprocheckStatus
        ]:
            reprocheck_status = ReprocheckStatus(status)
        else:
            try:
                pipeline_status = pipeline_service.validate_status(status)
            except ValueError as e:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                )

    if triggered_by:
        try:
            triggered_by = pipeline_service.validate_trigger_filter(triggered_by)
        except ValueError as e:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    async with get_db(use_replica=True) as db:
        pipelines = await pipeline_service.list_pipelines_with_filters(
            db=db,
            app_id=app_id,
            pipeline_type=type,
            status=pipeline_status,
            reprocheck_status=reprocheck_status,
            triggered_by=triggered_by,
            target_repo=target_repo,
            limit=limit or 10,
        )

        return [
            pipeline_service.pipeline_to_summary(pipeline) for pipeline in pipelines
        ]


@pipelines_router.get(
    "/pipelines/{pipeline_id:uuid}",
    response_model=PipelineResponse,
    status_code=http_status.HTTP_200_OK,
)
async def get_pipeline(
    pipeline_id: uuid.UUID,
):
    async with get_db(use_replica=True) as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        return pipeline_service.pipeline_to_response(pipeline)


@pipelines_router.post(
    "/pipelines/{pipeline_id:uuid}/callback/metadata",
    status_code=http_status.HTTP_200_OK,
)
async def pipeline_metadata_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    build_pipeline: VerifiedBuildPipeline,
):
    return await execute_callback_handler(
        build_pipeline.handle_metadata_callback,
        pipeline_id,
        data,
    )


@pipelines_router.post(
    "/pipelines/{pipeline_id:uuid}/callback/log_url",
    status_code=http_status.HTTP_200_OK,
)
async def pipeline_log_url_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    build_pipeline: VerifiedBuildPipeline,
):
    return await execute_callback_handler(
        build_pipeline.handle_log_url_callback,
        pipeline_id,
        data,
        conflict_messages=["Log URL already set"],
    )


@pipelines_router.post(
    "/pipelines/{pipeline_id:uuid}/callback/status",
    status_code=http_status.HTTP_200_OK,
)
async def pipeline_status_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    build_pipeline: VerifiedBuildPipeline,
):
    return await execute_callback_handler(
        build_pipeline.handle_status_callback,
        pipeline_id,
        data,
        conflict_messages=["Pipeline status already finalized"],
    )


@pipelines_router.post(
    "/pipelines/{pipeline_id:uuid}/callback/reprocheck",
    status_code=http_status.HTTP_200_OK,
)
async def pipeline_reprocheck_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    build_pipeline: VerifiedBuildPipeline,
):
    return await execute_callback_handler(
        build_pipeline.handle_reprocheck_callback,
        pipeline_id,
        data,
        conflict_messages=["Pipeline status already finalized"],
    )


@pipelines_router.post(
    "/pipelines/{pipeline_id:uuid}/callback/cost",
    status_code=http_status.HTTP_200_OK,
)
async def pipeline_cost_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    build_pipeline: VerifiedBuildPipeline,
):
    return await execute_callback_handler(
        build_pipeline.handle_cost_callback,
        pipeline_id,
        data,
    )


@pipelines_router.get(
    "/pipelines/{pipeline_id:uuid}/log_url",
)
async def redirect_to_log_url(
    pipeline_id: uuid.UUID,
    response: Response,
):
    async with get_db(use_replica=True) as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if not pipeline.log_url:
            response.status_code = http_status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = "10"
            return {"detail": "Log URL not available yet"}

        response.status_code = http_status.HTTP_307_TEMPORARY_REDIRECT
        response.headers["Location"] = pipeline.log_url
        return {}
