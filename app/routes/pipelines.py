import uuid
import secrets
from typing import Any, Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

from fastapi import APIRouter, HTTPException, status, Header, Depends

from app.database import get_db
from app.models import Pipeline, PipelineTrigger, PipelineStatus
from app.pipelines.build import BuildPipeline
from app.providers.factory import ProviderFactory
from app.providers.base import ProviderType
from app.config import settings
from sqlalchemy.future import select

pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])


async def verify_token(x_api_token: str = Header(...)):
    if x_api_token != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
    return x_api_token


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
    github_provider = await ProviderFactory.create_provider(ProviderType.GITHUB, {})
    pipeline_service = BuildPipeline(github_provider)

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
    x_callback_token: str = Header(..., alias="X-Callback-Token"),
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if not secrets.compare_digest(x_callback_token, pipeline.callback_token):
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

            github_provider = await ProviderFactory.create_provider(
                ProviderType.GITHUB, {}
            )
            pipeline_service = BuildPipeline(github_provider)

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
