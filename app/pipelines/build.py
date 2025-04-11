import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus
from app.providers import ProviderType, JobProvider
from app.config import settings


class BuildPipeline:
    def __init__(self, github_provider: JobProvider):
        self.github_provider = github_provider

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
            provider=ProviderType.GITHUB.value,
            provider_data={},
        )
        db.add(pipeline)
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

        job_data = {
            "app_id": pipeline.app_id,
            "job_type": "build",
            "params": {
                "owner": "flathub-infra",
                "repo": "vorarbeiter-stubs",
                "workflow_id": "build.yml",
                "ref": "main",
                "inputs": {
                    "app_id": pipeline.app_id,
                    "branch": pipeline.params.get("branch", "stable"),
                    "git_ref": pipeline.params.get("ref", "master"),
                    "build_url": str(pipeline.id),
                    "arches": "x86_64,aarch64",
                    "repo_token": "dummy-token",
                    "callback_url": f"{settings.base_url}/api/pipelines/{pipeline.id}/callback",
                    "callback_token": pipeline.callback_token,
                },
            },
        }

        provider_result = await self.github_provider.dispatch(
            str(pipeline.id), str(pipeline.id), job_data
        )

        pipeline.provider_data = provider_result

        return pipeline

    async def handle_callback(
        self,
        db: AsyncSession,
        pipeline_id: uuid.UUID,
        status: str,
        result: Dict[str, Any],
    ) -> Pipeline:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        if status.lower() == "success":
            pipeline.status = PipelineStatus.PUBLISHED
            pipeline.published_at = datetime.now()
            pipeline.finished_at = datetime.now()
            pipeline.result = result
        elif status.lower() == "failure":
            pipeline.status = PipelineStatus.FAILED
            pipeline.finished_at = datetime.now()
            pipeline.result = result
        elif status.lower() == "cancelled":
            pipeline.status = PipelineStatus.CANCELLED
            pipeline.finished_at = datetime.now()
            pipeline.result = result
        else:
            pipeline.status = PipelineStatus.PENDING
            pipeline.result = result

        return pipeline

    async def get_pipeline(
        self, db: AsyncSession, pipeline_id: uuid.UUID
    ) -> Optional[Pipeline]:
        return await db.get(Pipeline, pipeline_id)
