import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models import Pipeline, PipelineStatus, Job, JobStatus
from app.providers import ProviderType, JobProvider


class BuildPipeline:
    def __init__(self, github_provider: JobProvider):
        self.github_provider = github_provider
        self.job_sequence: Dict[str, Dict[str, Optional[str]]] = {
            "validate_manifest": {"next": "build"},
            "build": {"next": None},
        }

    async def create_pipeline(
        self,
        db: AsyncSession,
        app_id: str,
        params: Dict[str, Any],
        webhook_event_id: Optional[uuid.UUID] = None,
    ) -> Pipeline:
        pipeline = Pipeline(
            app_id=app_id,
            params=params,
            webhook_event_id=webhook_event_id,
        )
        db.add(pipeline)
        await db.flush()

        validate_job = Job(
            pipeline_id=pipeline.id,
            job_type="validate_manifest",
            status=JobStatus.PENDING,
            provider=ProviderType.GITHUB.value,
            provider_data={},
            position=0,
        )
        db.add(validate_job)
        await db.flush()

        return pipeline

    async def start_pipeline(
        self,
        db: AsyncSession,
        pipeline_id: uuid.UUID,
    ) -> Pipeline:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        if pipeline.status != PipelineStatus.PENDING:
            raise ValueError(f"Pipeline {pipeline_id} is not in PENDING state")

        pipeline.status = PipelineStatus.RUNNING
        pipeline.started_at = datetime.now()

        stmt = select(Job).where(
            Job.pipeline_id == pipeline_id, Job.job_type == "validate_manifest"
        )
        result = await db.execute(stmt)
        validate_job = result.scalar_one_or_none()

        if not validate_job:
            raise ValueError(f"Validate job not found for pipeline {pipeline_id}")

        validate_job.status = JobStatus.RUNNING
        validate_job.started_at = datetime.now()

        job_data = {
            "app_id": pipeline.app_id,
            "job_type": validate_job.job_type,
            "params": {
                "owner": "flathub-infra",
                "repo": "vorarbeiter-stubs",
                "workflow_id": "validate-manifest.yml",
                "ref": "main",
                "inputs": {
                    **pipeline.params,
                    "callback_url": f"/api/pipelines/{pipeline.id}/jobs/{validate_job.id}/callback",
                },
            },
        }

        provider_result = await self.github_provider.dispatch(
            str(validate_job.id), str(pipeline.id), job_data
        )

        validate_job.provider_data = provider_result

        await db.commit()
        return pipeline

    async def build(self, db: AsyncSession, job_id: uuid.UUID) -> Job:
        validate_job = await db.get(Job, job_id)
        if not validate_job:
            raise ValueError(f"Job {job_id} not found")

        pipeline = await db.get(Pipeline, validate_job.pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {validate_job.pipeline_id} not found")

        build_job = Job(
            pipeline_id=pipeline.id,
            job_type="build",
            status=JobStatus.PENDING,
            provider=ProviderType.GITHUB.value,
            provider_data={},
            position=1,
        )
        db.add(build_job)
        await db.flush()

        build_job.status = JobStatus.RUNNING
        build_job.started_at = datetime.now()

        job_data = {
            "app_id": pipeline.app_id,
            "job_type": build_job.job_type,
            "params": {
                "owner": "flathub-infra",
                "repo": "vorarbeiter-stubs",
                "workflow_id": "build.yml",
                "ref": "main",
                "inputs": {
                    **pipeline.params,
                    "callback_url": f"/api/pipelines/{pipeline.id}/jobs/{build_job.id}/callback",
                },
            },
        }

        provider_result = await self.github_provider.dispatch(
            str(build_job.id), str(pipeline.id), job_data
        )

        build_job.provider_data = provider_result

        await db.commit()
        return build_job

    async def publish(self, db: AsyncSession, build_job_id: uuid.UUID) -> Pipeline:
        build_job = await db.get(Job, build_job_id)
        if not build_job:
            raise ValueError(f"Job {build_job_id} not found")

        pipeline = await db.get(Pipeline, build_job.pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {build_job.pipeline_id} not found")

        pipeline.status = PipelineStatus.PUBLISHED
        pipeline.published_at = datetime.now()
        pipeline.finished_at = datetime.now()

        await db.commit()
        return pipeline

    async def handle_job_callback(
        self, db: AsyncSession, job_id: uuid.UUID, status: str, result: Dict[str, Any]
    ) -> Job:
        job = await db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        pipeline = await db.get(Pipeline, job.pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {job.pipeline_id} not found")

        if status.lower() == "success":
            job.status = JobStatus.COMPLETE
            job.finished_at = datetime.now()
            job.result = result

            job_type = job.job_type
            if job_type in self.job_sequence and self.job_sequence[job_type]["next"]:
                next_job_type = self.job_sequence[job_type]["next"]
                if job_type == "validate_manifest" and next_job_type == "build":
                    await self.build(db, job.id)

        elif status.lower() == "failure":
            job.status = JobStatus.FAILED
            job.finished_at = datetime.now()
            job.result = result
            pipeline.status = PipelineStatus.FAILED
            pipeline.finished_at = datetime.now()
        elif status.lower() == "cancelled":
            job.status = JobStatus.CANCELLED
            job.finished_at = datetime.now()
            job.result = result
            pipeline.status = PipelineStatus.CANCELLED
            pipeline.finished_at = datetime.now()
        else:
            job.status = JobStatus.PENDING
            job.result = result

        await db.commit()
        return job

    async def get_pipeline(
        self, db: AsyncSession, pipeline_id: uuid.UUID
    ) -> Optional[Pipeline]:
        return await db.get(Pipeline, pipeline_id)

    async def get_job(self, db: AsyncSession, job_id: uuid.UUID) -> Optional[Job]:
        return await db.get(Job, job_id)

    async def get_pipeline_jobs(
        self, db: AsyncSession, pipeline_id: uuid.UUID
    ) -> List[Job]:
        stmt = select(Job).where(Job.pipeline_id == pipeline_id).order_by(Job.position)
        result = await db.execute(stmt)
        return list(result.scalars().all())
