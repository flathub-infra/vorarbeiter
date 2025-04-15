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
from app.utils.github import update_commit_status, create_pr_comment
import logging

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
        if v not in ["success", "failure", "cancelled"]:
            raise ValueError("status must be 'success', 'failure', or 'cancelled'")
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
                PipelineStatus.SUCCEEDED,
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

            await pipeline_service.update_pipeline_status(
                pipeline=pipeline, status=status_value, result=result
            )

            if pipeline:
                repo = pipeline.params.get("repo")
                sha = pipeline.params.get("sha")

                if repo and sha:
                    match status_value:
                        case "success":
                            description = "Build succeeded."
                            github_state = "success"
                        case "failure":
                            description = "Build failed."
                            github_state = "failure"
                        case "cancelled":
                            description = "Build cancelled."
                            github_state = "failure"
                        case _:
                            description = f"Build status: {status_value}."
                            github_state = "failure"

                    target_url = pipeline.log_url
                    if not target_url:
                        logging.warning(
                            f"Pipeline {pipeline_id}: log_url is unexpectedly None when setting final commit status."
                        )
                        target_url = ""

                    await update_commit_status(
                        repo=repo,
                        sha=sha,
                        state=github_state,
                        description=description,
                        target_url=target_url,
                    )

                    pr_number_str = pipeline.params.get("pr_number")
                    if pr_number_str:
                        try:
                            pr_number = int(pr_number_str)
                            log_url = pipeline.log_url
                            comment = ""
                            if status_value == "success":
                                comment = f"🚧 [Test build]({log_url}) succeeded."
                            elif status_value == "failure":
                                comment = f"🚧 [Test build]({log_url}) failed."
                            elif status_value == "cancelled":
                                comment = "🚧 [Test build]({log_url}) was cancelled."

                            if comment:
                                await create_pr_comment(
                                    repo=repo,
                                    pr_number=pr_number,
                                    comment=comment,
                                )
                        except ValueError:
                            logging.error(
                                f"Invalid pr_number '{pr_number_str}' for pipeline {pipeline_id}. Skipping final PR comment."
                            )
                        except Exception as e:
                            logging.error(
                                f"Error creating final PR comment for pipeline {pipeline_id}: {e}"
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

            repo = pipeline.params.get("repo")
            sha = pipeline.params.get("sha")
            pr_number_str = pipeline.params.get("pr_number")

            if repo and sha:
                target_url = (
                    pipeline.log_url
                    if pipeline.log_url
                    else f"{settings.base_url}/api/pipelines/{pipeline_id}"
                )
                await update_commit_status(
                    repo=repo,
                    sha=sha,
                    state="pending",
                    description="Build in progress",
                    target_url=target_url,
                )

                if pr_number_str and pipeline.log_url:
                    try:
                        pr_number = int(pr_number_str)
                        comment = f"Started [test build]({pipeline.log_url})."
                        await create_pr_comment(
                            repo=repo,
                            pr_number=pr_number,
                            comment=comment,
                        )
                    except ValueError:
                        logging.error(
                            f"Invalid pr_number '{pr_number_str}' for pipeline {pipeline_id}. Skipping PR comment."
                        )
                    except Exception as e:
                        logging.error(
                            f"Error creating 'Started' PR comment for pipeline {pipeline_id}: {e}"
                        )

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
