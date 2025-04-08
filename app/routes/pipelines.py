import uuid
from typing import Any, Dict
from datetime import datetime

from fastapi import APIRouter, HTTPException, status

from app.database import get_db
from app.models import Job, JobStatus, Pipeline, PipelineStatus

pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])


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
