import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, status, Header, Depends

from app.database import get_db
from app.models import Job, JobStatus, Pipeline, PipelineStatus, PipelineTrigger
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


class JobResponse(BaseModel):
    id: str
    job_type: str
    status: str
    position: int
    provider: str
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None


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
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    jobs: List[JobResponse] = []


async def process_job_status(pipeline_id: uuid.UUID, job_id: uuid.UUID):
    async with get_db() as db:
        job = await db.get(Job, job_id)
        if not job:
            return

        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            return

        if job.status == JobStatus.FAILED:
            pipeline.status = PipelineStatus.FAILED
            pipeline.finished_at = datetime.now()
        elif job.status == JobStatus.CANCELLED:
            pipeline.status = PipelineStatus.CANCELLED
            pipeline.finished_at = datetime.now()


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

        await db.commit()

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

        stmt = select(Job).where(Job.pipeline_id == pipeline_id).order_by(Job.position)
        result = await db.execute(stmt)
        jobs = list(result.scalars().all())

        return PipelineResponse(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status.value,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by.value,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
            jobs=[
                JobResponse(
                    id=str(job.id),
                    job_type=job.job_type,
                    status=job.status.value,
                    position=job.position,
                    provider=job.provider,
                    created_at=job.created_at,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                    result=job.result,
                )
                for job in jobs
            ],
        )


@pipelines_router.get(
    "/pipelines/{pipeline_id}/jobs/{job_id}",
    response_model=JobResponse,
    status_code=status.HTTP_200_OK,
)
async def get_job(
    pipeline_id: uuid.UUID,
    job_id: uuid.UUID,
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        job = await db.get(Job, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found",
            )

        if job.pipeline_id != pipeline.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} does not belong to pipeline {pipeline_id}",
            )

        return JobResponse(
            id=str(job.id),
            job_type=job.job_type,
            status=job.status.value,
            position=job.position,
            provider=job.provider,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            result=job.result,
        )


@pipelines_router.get(
    "/pipelines/{pipeline_id}/jobs",
    response_model=List[JobResponse],
    status_code=status.HTTP_200_OK,
)
async def list_pipeline_jobs(
    pipeline_id: uuid.UUID,
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        stmt = select(Job).where(Job.pipeline_id == pipeline_id).order_by(Job.position)
        result = await db.execute(stmt)
        jobs = list(result.scalars().all())

        return [
            JobResponse(
                id=str(job.id),
                job_type=job.job_type,
                status=job.status.value,
                position=job.position,
                provider=job.provider,
                created_at=job.created_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                result=job.result,
            )
            for job in jobs
        ]


@pipelines_router.post(
    "/pipelines/{pipeline_id}/jobs/{job_id}/callback",
    status_code=status.HTTP_200_OK,
)
async def job_callback(
    pipeline_id: uuid.UUID,
    job_id: uuid.UUID,
    data: Dict[str, Any],
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        job = await db.get(Job, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found",
            )

        if job.pipeline_id != pipeline.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Job {job_id} does not belong to pipeline {pipeline_id}",
            )

        job_status = data.get("status", "failure").lower()
        job_result = data.get("result", {})

        match job_status:
            case "success":
                job.status = JobStatus.COMPLETE
                job.finished_at = datetime.now()
            case "failure":
                job.status = JobStatus.FAILED
                job.finished_at = datetime.now()
            case "cancelled":
                job.status = JobStatus.CANCELLED
                job.finished_at = datetime.now()
            case _:
                job.status = JobStatus.PENDING

        job.result = job_result

        await process_job_status(pipeline.id, job.id)

    return {
        "status": "ok",
        "job_id": str(job_id),
        "pipeline_id": str(pipeline_id),
        "job_status": job_status,
    }
