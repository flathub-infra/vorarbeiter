import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.providers import ProviderType, get_provider


class BuildPipeline:
    def __init__(self):
        self.github_provider = get_provider(ProviderType.GITHUB)

    async def create_pipeline(
        self,
        app_id: str,
        params: Dict[str, Any],
        webhook_event_id: Optional[uuid.UUID] = None,
    ) -> Pipeline:
        async with get_db() as db:
            pipeline = Pipeline(
                app_id=app_id,
                params=params,
                webhook_event_id=webhook_event_id,
                provider=ProviderType.GITHUB.value,
                provider_data={},
            )
            db.add(pipeline)
            await db.flush()
            await db.commit()
            return pipeline

    async def start_pipeline(
        self,
        pipeline_id: uuid.UUID,
    ) -> Pipeline:
        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            if pipeline.status != PipelineStatus.PENDING:
                raise ValueError(f"Pipeline {pipeline_id} is not in PENDING state")

            pipeline.status = PipelineStatus.RUNNING
            pipeline.started_at = datetime.now()

            source_branch = pipeline.params.get("branch")
            match source_branch:
                case "master":
                    branch = "stable"
                case "beta":
                    branch = "beta"
                case source_branch if isinstance(
                    source_branch, str
                ) and source_branch.startswith("branch/"):
                    branch = source_branch.removeprefix("branch/")
                case _:
                    branch = "test"

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
                        "branch": branch,
                        "git_ref": pipeline.params.get("ref", "master"),
                        "build_url": str(pipeline.id),
                        "arches": "x86_64,aarch64",
                        "repo_token": settings.repo_token,
                        "callback_url": f"{settings.base_url}/api/pipelines/{pipeline.id}/callback",
                        "callback_token": pipeline.callback_token,
                    },
                },
            }

            provider_result = await self.github_provider.dispatch(
                str(pipeline.id), str(pipeline.id), job_data
            )

            pipeline.provider_data = provider_result

            await db.commit()
            return pipeline

    async def handle_callback(
        self,
        pipeline_id: uuid.UUID,
        status: str,
        result: Dict[str, Any],
    ) -> Pipeline:
        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            match status.lower():
                case "success":
                    pipeline.status = PipelineStatus.SUCCEEDED
                    pipeline.finished_at = datetime.now()
                    pipeline.result = result
                case "failure":
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.finished_at = datetime.now()
                    pipeline.result = result
                case "cancelled":
                    pipeline.status = PipelineStatus.CANCELLED
                    pipeline.finished_at = datetime.now()
                    pipeline.result = result
                case _:
                    pipeline.status = PipelineStatus.PENDING
                    pipeline.result = result

            await db.commit()
            return pipeline

    async def get_pipeline(self, pipeline_id: uuid.UUID) -> Optional[Pipeline]:
        async with get_db() as db:
            return await db.get(Pipeline, pipeline_id)
