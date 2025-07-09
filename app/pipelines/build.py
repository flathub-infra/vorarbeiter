import secrets
import uuid
from datetime import datetime
from typing import Any, Optional

import httpx
import structlog
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.services import github_actions_service
from app.services.callback import CallbackValidator
from app.services.github_notifier import GitHubNotifier
from app.utils.flat_manager import FlatManagerClient

logger = structlog.get_logger(__name__)


class CallbackData(BaseModel):
    status: Optional[str] = None
    log_url: Optional[str] = None
    app_id: Optional[str] = None
    is_extra_data: Optional[bool] = None
    end_of_life: Optional[str] = None
    end_of_life_rebase: Optional[str] = None


app_build_types = {
    "io.github.ungoogled_software.ungoogled_chromium": "large",
    "org.chromium.Chromium": "large",
    "com.adamcake.Bolt": "large",
    "org.libreoffice.LibreOffice": "large",
    "org.freecad.FreeCAD": "large",
    "org.freedesktop.LinuxAudio.Plugins.ChowDSP-Plugins": "large",
    "org.paraview.ParaView": "large",
    "com.bambulab.BambuStudio": "large",
    "org.telegram.desktop": "large",
    "org.gnome.Fractal": "large",
    "com.pot_app.pot": "large",
    "org.mamedev.MAME": "medium",
}


class BuildPipeline:
    def __init__(self):
        self.provider = github_actions_service
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
                if build_id is None:
                    raise ValueError(
                        "Failed to get build ID from flat-manager response"
                    )

                pipeline.build_id = build_id

                upload_token = await self.flat_manager.create_token_subset(
                    build_id=build_id, app_id=pipeline.app_id
                )
            except Exception as e:
                raise ValueError(f"Failed to create build in flat-manager: {str(e)}")

            workflow_id = pipeline.params.get("workflow_id", "build.yml")

            if pipeline.app_id in app_build_types:
                build_type = app_build_types[pipeline.app_id]
            else:
                build_type = pipeline.params.get("build_type", "default")

            pipeline.params["build_type"] = build_type

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
                        "build_type": build_type,
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
        callback_data: CallbackData | dict[str, Any],
    ) -> tuple[Pipeline, dict[str, Any]]:
        if isinstance(callback_data, dict):
            validator = CallbackValidator()
            callback_data = validator.validate_and_parse(callback_data)
        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            updates: dict[str, Any] = {}
            if pipeline.app_id == "flathub" and callback_data.app_id:
                pipeline.app_id = callback_data.app_id
                updates["app_id"] = pipeline.app_id

            if callback_data.is_extra_data is not None:
                pipeline.is_extra_data = callback_data.is_extra_data
                updates["is_extra_data"] = pipeline.is_extra_data

            if callback_data.end_of_life:
                pipeline.end_of_life = callback_data.end_of_life
                updates["end_of_life"] = pipeline.end_of_life

            if callback_data.end_of_life_rebase:
                pipeline.end_of_life_rebase = callback_data.end_of_life_rebase
                updates["end_of_life_rebase"] = pipeline.end_of_life_rebase
            if callback_data.log_url:
                if pipeline.log_url:
                    raise ValueError("Log URL already set")
                pipeline.log_url = callback_data.log_url
                updates["log_url"] = pipeline.log_url
                await db.commit()
                github_notifier = GitHubNotifier()
                await github_notifier.handle_build_started(
                    pipeline, callback_data.log_url
                )

                return pipeline, updates
            if callback_data.status:
                if pipeline.status in [
                    PipelineStatus.SUCCEEDED,
                    PipelineStatus.PUBLISHED,
                ]:
                    raise ValueError("Pipeline status already finalized")

                status_value = callback_data.status.lower()
                if status_value not in ["success", "failure", "cancelled"]:
                    raise ValueError(
                        "status must be 'success', 'failure', or 'cancelled'"
                    )

                match status_value:
                    case "success":
                        pipeline.status = PipelineStatus.SUCCEEDED
                        pipeline.finished_at = datetime.now()
                    case "failure":
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.finished_at = datetime.now()
                    case "cancelled":
                        pipeline.status = PipelineStatus.CANCELLED
                        pipeline.finished_at = datetime.now()

                await db.commit()
                if status_value == "success":
                    flat_manager = None
                    if pipeline.params.get("pr_number"):
                        flat_manager = FlatManagerClient(
                            url=settings.flat_manager_url,
                            token=settings.flat_manager_token,
                        )
                    github_notifier = GitHubNotifier(flat_manager_client=flat_manager)
                    await github_notifier.handle_build_completion(
                        pipeline, status_value, flat_manager_client=flat_manager
                    )
                    if pipeline.build_id:
                        try:
                            if not flat_manager:
                                flat_manager = FlatManagerClient(
                                    url=settings.flat_manager_url,
                                    token=settings.flat_manager_token,
                                )
                            await flat_manager.commit(
                                pipeline.build_id,
                                end_of_life=pipeline.end_of_life,
                                end_of_life_rebase=pipeline.end_of_life_rebase,
                            )
                            logger.info(
                                "Committed build",
                                build_id=pipeline.build_id,
                                pipeline_id=str(pipeline_id),
                            )

                            try:
                                build_info = await flat_manager.get_build_info(
                                    pipeline.build_id
                                )
                                build_data = build_info.get("build", {})
                                commit_job_id = build_data.get("commit_job_id")
                                if commit_job_id and not pipeline.commit_job_id:
                                    pipeline.commit_job_id = commit_job_id
                                    await db.commit()
                                    logger.info(
                                        "Stored commit job ID",
                                        commit_job_id=commit_job_id,
                                        pipeline_id=str(pipeline_id),
                                    )
                                    if pipeline.flat_manager_repo in ["stable", "beta"]:
                                        await github_notifier.notify_flat_manager_job_status(
                                            pipeline,
                                            "commit",
                                            commit_job_id,
                                            "pending",
                                            "Committing build...",
                                        )
                            except Exception as e:
                                logger.warning(
                                    "Failed to fetch commit job ID after commit",
                                    pipeline_id=str(pipeline_id),
                                    error=str(e),
                                )
                        except httpx.HTTPStatusError as e:
                            logger.error(
                                "Failed to commit build",
                                build_id=pipeline.build_id,
                                pipeline_id=str(pipeline_id),
                                status_code=e.response.status_code,
                                response_text=e.response.text,
                            )
                        except Exception as e:
                            logger.error(
                                "Unexpected error while committing build",
                                build_id=pipeline.build_id,
                                pipeline_id=str(pipeline_id),
                                error=str(e),
                            )
                    else:
                        logger.warning(
                            "Pipeline succeeded but has no build_id, skipping commit",
                            pipeline_id=str(pipeline_id),
                        )
                else:
                    github_notifier = GitHubNotifier()
                    await github_notifier.handle_build_completion(
                        pipeline, status_value, flat_manager_client=None
                    )

                updates["pipeline_status"] = status_value
                return pipeline, updates
            await db.commit()
            return pipeline, updates

    async def get_pipeline(self, pipeline_id: uuid.UUID) -> Pipeline | None:
        async with get_db() as db:
            return await db.get(Pipeline, pipeline_id)

    async def verify_callback_token(
        self, pipeline_id: uuid.UUID, token: str
    ) -> Pipeline:
        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            if not secrets.compare_digest(token, pipeline.callback_token):
                raise ValueError("Invalid callback token")

            return pipeline
