import uuid
from datetime import datetime
from typing import Any

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.providers import github_provider
from app.providers.base import ProviderType  # Kept for backward compatibility
from app.utils.flat_manager import FlatManagerClient


class BuildPipeline:
    def __init__(self):
        self.provider = github_provider
        self.flat_manager = FlatManagerClient(
            url=settings.flat_manager_url, token=settings.flat_manager_token
        )

    async def create_pipeline(
        self,
        app_id: str,
        params: dict[str, Any],
        webhook_event_id: uuid.UUID | None = None,
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

            ref = pipeline.params.get("ref")
            match ref:
                case "refs/heads/master":
                    flat_manager_repo = "stable"
                case "refs/heads/beta":
                    flat_manager_repo = "beta"
                case ref if isinstance(ref, str) and ref.startswith(
                    "refs/heads/branch/"
                ):
                    flat_manager_repo = "stable"
                case _:
                    flat_manager_repo = "test"

            pipeline.flat_manager_repo = flat_manager_repo

            build_log_url = f"{settings.base_url}/api/pipelines/{pipeline.id}/log_url"

            try:
                build_data = await self.flat_manager.create_build(
                    repo=flat_manager_repo, build_log_url=build_log_url
                )

                build_id = build_data.get("id")
                pipeline.build_id = build_id

                upload_token = await self.flat_manager.create_token_subset(
                    build_id=build_id, app_id=pipeline.app_id
                )
            except Exception as e:
                raise ValueError(f"Failed to create build in flat-manager: {str(e)}")

            workflow_id = pipeline.params.get("workflow_id", "build.yml")

            job_data = {
                "app_id": pipeline.app_id,
                "job_type": "build",
                "params": {
                    "owner": "flathub-infra",
                    "repo": "vorarbeiter",
                    "workflow_id": workflow_id,
                    "ref": "main",
                    "inputs": {
                        "app_id": pipeline.app_id,
                        "git_ref": pipeline.params.get("ref", "master"),
                        "build_url": self.flat_manager.get_build_url(build_id),
                        "flat_manager_repo": flat_manager_repo,
                        "flat_manager_token": upload_token,
                        "callback_url": f"{settings.base_url}/api/pipelines/{pipeline.id}/callback",
                        "callback_token": pipeline.callback_token,
                        "build_type": pipeline.params.get("build_type", "default"),
                    },
                },
            }

            provider_result = await self.provider.dispatch(
                str(pipeline.id), str(pipeline.id), job_data
            )

            pipeline.provider_data = provider_result

            await db.commit()
            return pipeline

    async def handle_callback(
        self,
        pipeline_id: uuid.UUID,
        status: str,
    ) -> Pipeline:
        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            log_url = pipeline.log_url

            match status.lower():
                case "success":
                    pipeline.status = PipelineStatus.SUCCEEDED
                    pipeline.finished_at = datetime.now()
                case "failure":
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.finished_at = datetime.now()
                case "cancelled":
                    pipeline.status = PipelineStatus.CANCELLED
                    pipeline.finished_at = datetime.now()
                case _:
                    pipeline.status = PipelineStatus.PENDING

            if log_url and not pipeline.log_url:
                pipeline.log_url = log_url

            await db.commit()
            return pipeline

    async def get_pipeline(self, pipeline_id: uuid.UUID) -> Pipeline | None:
        async with get_db() as db:
            return await db.get(Pipeline, pipeline_id)
