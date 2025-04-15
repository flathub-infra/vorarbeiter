import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

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
                    flat_manager_repo = "stable"
                case "beta":
                    flat_manager_repo = "beta"
                case source_branch if isinstance(
                    source_branch, str
                ) and source_branch.startswith("branch/"):
                    flat_manager_repo = "stable"
                case _:
                    flat_manager_repo = "test"

            build_log_url = f"{settings.base_url}/api/pipelines/{pipeline.id}/log_url"

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{settings.flat_manager_url}/api/v1/build",
                        json={
                            "repo": flat_manager_repo,
                            "build-log-url": build_log_url,
                        },
                        headers={
                            "Authorization": f"Bearer {settings.flat_manager_token}"
                        },
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    build_data = response.json()
                    build_id = build_data.get("id")
                    build_url = f"{settings.flat_manager_url}/api/v1/build/{build_id}"
                    pipeline.build_url = build_url

                    upload_token_response = await client.post(
                        f"{settings.flat_manager_url}/api/v1/token_subset",
                        json={
                            "name": "upload",
                            "sub": f"build/{build_id}",
                            "scope": ["upload"],
                            "prefix": [pipeline.app_id],
                            "duration": 6 * 60 * 60,
                        },
                        headers={
                            "Authorization": f"Bearer {settings.flat_manager_token}"
                        },
                        timeout=30.0,
                    )
                    upload_token_response.raise_for_status()
                    upload_token = upload_token_response.json().get("token")
            except Exception as e:
                raise ValueError(f"Failed to create build in flat-manager: {str(e)}")

            job_data = {
                "app_id": pipeline.app_id,
                "job_type": "build",
                "params": {
                    "owner": "flathub-infra",
                    "repo": "vorarbeiter",
                    "workflow_id": "build.yml",
                    "ref": "main",
                    "inputs": {
                        "app_id": pipeline.app_id,
                        "git_ref": pipeline.params.get("ref", "master"),
                        "build_url": build_url,
                        "flat_manager_repo": flat_manager_repo,
                        "flat_manager_token": upload_token,
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

            log_url = pipeline.log_url
            build_url = pipeline.build_url

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

            if log_url and not pipeline.log_url:
                pipeline.log_url = log_url
            if build_url and not pipeline.build_url:
                pipeline.build_url = build_url

            await db.commit()
            return pipeline

    async def get_pipeline(self, pipeline_id: uuid.UUID) -> Optional[Pipeline]:
        async with get_db() as db:
            return await db.get(Pipeline, pipeline_id)
