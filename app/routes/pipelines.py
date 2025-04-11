import uuid
import secrets
from typing import Any, Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

from fastapi import APIRouter, HTTPException, status, Depends, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.database import get_db
from app.models import Pipeline, PipelineTrigger, PipelineStatus
from app.pipelines import BuildPipeline, ensure_providers_initialized
from app.config import settings
from sqlalchemy.future import select

pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])
security = HTTPBearer()
api_security = HTTPBearer()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(api_security),
):
    if credentials.credentials != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
    return credentials.credentials


class PipelineTriggerRequest(BaseModel):
    app_id: str
    params: Dict[str, Any]


class PipelineSummary(BaseModel):
    id: str
    app_id: str
    status: str
    triggered_by: str
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class PipelineResponse(BaseModel):
    id: str
    app_id: str
    status: str
    params: Dict[str, Any]
    triggered_by: str
    provider: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    log_url: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class PipelineStatusCallback(BaseModel):
    status: str
    result: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def status_must_be_valid(cls, v):
        if v not in ["success", "failure"]:
            raise ValueError("status must be 'success' or 'failure'")
        return v


class PipelineLogUrlCallback(BaseModel):
    log_url: str


@pipelines_router.post(
    "/pipelines",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def trigger_pipeline(
    data: PipelineTriggerRequest,
    token: str = Depends(verify_token),
):
    await ensure_providers_initialized()
    pipeline_service = BuildPipeline()

    async with get_db() as db:
        pipeline = await pipeline_service.create_pipeline(
            db=db,
            app_id=data.app_id,
            params=data.params,
            webhook_event_id=None,
        )

        pipeline.triggered_by = PipelineTrigger.MANUAL
        await db.flush()

        pipeline = await pipeline_service.start_pipeline(
            db=db,
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
    response_model=List[PipelineSummary],
    status_code=status.HTTP_200_OK,
)
async def list_pipelines():
    async with get_db() as db:
        stmt = select(Pipeline).order_by(Pipeline.created_at.desc())
        result = await db.execute(stmt)
        pipelines = list(result.scalars().all())

        return [
            PipelineSummary(
                id=str(pipeline.id),
                app_id=pipeline.app_id,
                status=pipeline.status.value,
                triggered_by=pipeline.triggered_by.value,
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

        return PipelineResponse(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status.value,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by.value,
            provider=pipeline.provider,
            result=pipeline.result,
            log_url=pipeline.log_url,
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
    data: Dict[str, Any],
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
            if pipeline.status in [
                PipelineStatus.COMPLETE,
                PipelineStatus.FAILED,
                PipelineStatus.CANCELLED,
                PipelineStatus.PUBLISHED,
            ]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Pipeline status already finalized",
                )

            status_callback = PipelineStatusCallback(**data)
            status_value = status_callback.status.lower()
            result = status_callback.result

            await ensure_providers_initialized()
            pipeline_service = BuildPipeline()

            await pipeline_service.handle_callback(
                db=db, pipeline_id=pipeline_id, status=status_value, result=result
            )

            return {
                "status": "ok",
                "pipeline_id": str(pipeline_id),
                "pipeline_status": status_value,
            }

        elif "log_url" in data:
            if pipeline.log_url:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Log URL already set",
                )

            log_url_callback = PipelineLogUrlCallback(**data)
            pipeline.log_url = log_url_callback.log_url
            await db.flush()

            return {
                "status": "ok",
                "pipeline_id": str(pipeline_id),
                "log_url": pipeline.log_url,
            }

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request must contain either 'status' or 'log_url' field",
            )


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
