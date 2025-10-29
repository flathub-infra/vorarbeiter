import secrets
import uuid
from datetime import datetime
from typing import Any, Optional

import httpx
import structlog
from pydantic import BaseModel
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.services import github_actions_service
from app.services.github_actions import GitHubActionsService
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
    build_pipeline_id: Optional[str] = None


app_build_types = {
    "io.github.ungoogled_software.ungoogled_chromium": "large",
    "org.chromium.Chromium": "large",
    "com.adamcake.Bolt": "large",
    "org.libreoffice.LibreOffice": "large",
    "org.freecad.FreeCAD": "large",
    "org.freedesktop.LinuxAudio.Plugins.ChowDSP-Plugins": "large",
    "org.paraview.ParaView": "large",
    "com.bambulab.BambuStudio": "large",
    "org.gnome.Fractal": "large",
    "com.pot_app.pot": "large",
    "org.mamedev.MAME": "large",
    "org.catacombing.kumo": "large",
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

            if pipeline.app_id in app_build_types:
                build_type = app_build_types[pipeline.app_id]
            else:
                build_type = "medium"

            pipeline.params["build_type"] = build_type
            if hasattr(pipeline, "_sa_instance_state"):
                flag_modified(pipeline, "params")

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
            workflow_id = pipeline.params.get("workflow_id", "build.yml")
            build_type = pipeline.params["build_type"]

            requires_flat_manager = workflow_id != "reprocheck.yml"

            build_log_url = f"{settings.base_url}/api/pipelines/{pipeline.id}/log_url"

            upload_token = None
            if requires_flat_manager:
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
                    raise ValueError(
                        f"Failed to create build in flat-manager: {str(e)}"
                    )

            inputs = {
                "app_id": pipeline.app_id,
                "git_ref": pipeline.params.get("ref", "master"),
                "callback_url": f"{settings.base_url}/api/pipelines/{pipeline.id}/callback",
                "callback_token": pipeline.callback_token,
                "build_type": build_type,
                "spot": "true" if pipeline.params.get("use_spot", True) else "false",
            }

            if requires_flat_manager:
                assert pipeline.build_id is not None
                inputs.update(
                    {
                        "build_url": self.flat_manager.get_build_url(pipeline.build_id),
                        "flat_manager_repo": flat_manager_repo,
                        "flat_manager_token": upload_token,
                    }
                )

            if workflow_id == "reprocheck.yml":
                build_pipeline_id = pipeline.params.get("build_pipeline_id")
                if build_pipeline_id:
                    inputs["build_pipeline_id"] = build_pipeline_id

            job_data = {
                "app_id": pipeline.app_id,
                "job_type": "build",
                "params": {
                    "owner": "flathub-infra",
                    "repo": "vorarbeiter",
                    "workflow_id": workflow_id,
                    "ref": "main",
                    "inputs": inputs,
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

                try:
                    run_id = callback_data.log_url.rstrip("/").split("/")[-1]
                    if pipeline.provider_data is None:
                        pipeline.provider_data = {}
                    pipeline.provider_data["run_id"] = run_id
                    flag_modified(pipeline, "provider_data")
                except (IndexError, AttributeError):
                    logger.warning(
                        "Failed to extract run_id from log_url",
                        log_url=callback_data.log_url,
                        pipeline_id=str(pipeline_id),
                    )

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
                        github_actions = GitHubActionsService()
                        try:
                            was_cancelled = (
                                await github_actions.check_run_was_cancelled(
                                    pipeline.provider_data
                                )
                            )
                            if was_cancelled:
                                logger.info(
                                    "Build reclassified from failed to cancelled",
                                    pipeline_id=str(pipeline_id),
                                )
                                pipeline.status = PipelineStatus.CANCELLED
                                status_value = "cancelled"
                            else:
                                pipeline.status = PipelineStatus.FAILED
                        except Exception as e:
                            logger.warning(
                                "Failed to check if build was cancelled, treating as failed",
                                pipeline_id=str(pipeline_id),
                                error=str(e),
                            )
                            pipeline.status = PipelineStatus.FAILED
                        pipeline.finished_at = datetime.now()
                    case "cancelled":
                        pipeline.status = PipelineStatus.CANCELLED
                        pipeline.finished_at = datetime.now()

                await db.commit()

                if (
                    status_value == "cancelled"
                    and pipeline.flat_manager_repo in ["stable", "beta"]
                    and not pipeline.params.get("auto_retried")
                ):
                    retry_params = pipeline.params.copy()
                    retry_params["auto_retried"] = True
                    retry_params["use_spot"] = False

                    try:
                        retry_pipeline = await self.create_pipeline(
                            app_id=pipeline.app_id,
                            params=retry_params,
                            webhook_event_id=pipeline.webhook_event_id,
                        )
                        retry_pipeline = await self.start_pipeline(
                            pipeline_id=retry_pipeline.id
                        )
                        logger.info(
                            "Auto-retrying cancelled build",
                            original_pipeline_id=str(pipeline_id),
                            retry_pipeline_id=str(retry_pipeline.id),
                            flat_manager_repo=pipeline.flat_manager_repo,
                        )
                        return pipeline, updates
                    except Exception as e:
                        logger.error(
                            "Failed to auto-retry cancelled build",
                            pipeline_id=str(pipeline_id),
                            error=str(e),
                        )
                if (
                    status_value == "success"
                    and pipeline.params.get("workflow_id", "build.yml") == "build.yml"
                ):
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
                elif pipeline.params.get("workflow_id", "build.yml") == "build.yml":
                    github_notifier = GitHubNotifier()
                    await github_notifier.handle_build_completion(
                        pipeline, status_value, flat_manager_client=None
                    )

                if pipeline.params.get("workflow_id") == "reprocheck.yml":
                    logger.info(
                        "Processing reprocheck callback",
                        pipeline_id=str(pipeline_id),
                        has_build_pipeline_id=hasattr(
                            callback_data, "build_pipeline_id"
                        ),
                        build_pipeline_id=getattr(
                            callback_data, "build_pipeline_id", None
                        ),
                    )
                if (
                    pipeline.params.get("workflow_id") == "reprocheck.yml"
                    and hasattr(callback_data, "build_pipeline_id")
                    and callback_data.build_pipeline_id
                ):
                    try:
                        build_pipeline_id = uuid.UUID(callback_data.build_pipeline_id)
                        original_pipeline = await db.get(Pipeline, build_pipeline_id)
                        if (
                            original_pipeline
                            and not original_pipeline.repro_pipeline_id
                        ):
                            original_pipeline.repro_pipeline_id = pipeline.id
                            flag_modified(original_pipeline, "repro_pipeline_id")
                            logger.info(
                                "Updated original pipeline with reprocheck pipeline ID",
                                original_pipeline_id=str(build_pipeline_id),
                                reprocheck_pipeline_id=str(pipeline.id),
                            )
                    except (ValueError, TypeError) as e:
                        logger.error(
                            "Invalid build_pipeline_id in reprocheck callback",
                            build_pipeline_id=callback_data.build_pipeline_id,
                            error=str(e),
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to update original pipeline with reprocheck ID",
                            build_pipeline_id=callback_data.build_pipeline_id,
                            reprocheck_pipeline_id=str(pipeline.id),
                            error=str(e),
                        )

                updates["pipeline_status"] = status_value
            await db.commit()
            return pipeline, updates

    async def handle_metadata_callback(
        self,
        pipeline_id: uuid.UUID,
        callback_data: dict[str, Any],
    ) -> tuple[Pipeline, dict[str, Any]]:
        from app.services.callback import MetadataCallbackValidator

        validator = MetadataCallbackValidator()
        parsed_data = validator.validate_and_parse(callback_data)

        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            updates: dict[str, Any] = {}

            if pipeline.app_id == "flathub" and parsed_data.app_id:
                pipeline.app_id = parsed_data.app_id
                updates["app_id"] = pipeline.app_id

            if parsed_data.is_extra_data is not None:
                pipeline.is_extra_data = parsed_data.is_extra_data
                updates["is_extra_data"] = pipeline.is_extra_data

            if parsed_data.end_of_life:
                pipeline.end_of_life = parsed_data.end_of_life
                updates["end_of_life"] = pipeline.end_of_life

            if parsed_data.end_of_life_rebase:
                pipeline.end_of_life_rebase = parsed_data.end_of_life_rebase
                updates["end_of_life_rebase"] = pipeline.end_of_life_rebase

            await db.commit()
            return pipeline, updates

    async def handle_log_url_callback(
        self,
        pipeline_id: uuid.UUID,
        callback_data: dict[str, Any],
    ) -> tuple[Pipeline, dict[str, Any]]:
        from app.services.callback import LogUrlCallbackValidator

        validator = LogUrlCallbackValidator()
        parsed_data = validator.validate_and_parse(callback_data)
        assert parsed_data.log_url is not None

        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            if pipeline.log_url:
                raise ValueError("Log URL already set")

            updates: dict[str, Any] = {}
            pipeline.log_url = parsed_data.log_url
            updates["log_url"] = pipeline.log_url

            try:
                run_id = parsed_data.log_url.rstrip("/").split("/")[-1]
                if pipeline.provider_data is None:
                    pipeline.provider_data = {}
                pipeline.provider_data["run_id"] = run_id
                flag_modified(pipeline, "provider_data")
            except (IndexError, AttributeError):
                logger.warning(
                    "Failed to extract run_id from log_url",
                    log_url=parsed_data.log_url,
                    pipeline_id=str(pipeline_id),
                )

            await db.commit()
            github_notifier = GitHubNotifier()
            await github_notifier.handle_build_started(pipeline, parsed_data.log_url)

            return pipeline, updates

    async def handle_status_callback(
        self,
        pipeline_id: uuid.UUID,
        callback_data: dict[str, Any],
    ) -> tuple[Pipeline, dict[str, Any]]:
        from app.services.callback import StatusCallbackValidator

        validator = StatusCallbackValidator()
        parsed_data = validator.validate_and_parse(callback_data)
        assert parsed_data.status is not None

        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            if pipeline.status in [
                PipelineStatus.SUCCEEDED,
                PipelineStatus.PUBLISHED,
            ]:
                raise ValueError("Pipeline status already finalized")

            status_value = parsed_data.status.lower()
            if status_value not in ["success", "failure", "cancelled"]:
                raise ValueError("status must be 'success', 'failure', or 'cancelled'")

            updates: dict[str, Any] = {}

            match status_value:
                case "success":
                    pipeline.status = PipelineStatus.SUCCEEDED
                    pipeline.finished_at = datetime.now()
                case "failure":
                    github_actions = GitHubActionsService()
                    try:
                        was_cancelled = await github_actions.check_run_was_cancelled(
                            pipeline.provider_data
                        )
                        if was_cancelled:
                            logger.info(
                                "Build reclassified from failed to cancelled",
                                pipeline_id=str(pipeline_id),
                            )
                            pipeline.status = PipelineStatus.CANCELLED
                            status_value = "cancelled"
                        else:
                            pipeline.status = PipelineStatus.FAILED
                    except Exception as e:
                        logger.warning(
                            "Failed to check if build was cancelled, treating as failed",
                            pipeline_id=str(pipeline_id),
                            error=str(e),
                        )
                        pipeline.status = PipelineStatus.FAILED
                    pipeline.finished_at = datetime.now()
                case "cancelled":
                    pipeline.status = PipelineStatus.CANCELLED
                    pipeline.finished_at = datetime.now()

            await db.commit()

            if (
                status_value == "cancelled"
                and pipeline.flat_manager_repo in ["stable", "beta"]
                and not pipeline.params.get("auto_retried")
            ):
                retry_params = pipeline.params.copy()
                retry_params["auto_retried"] = True
                retry_params["use_spot"] = False

                try:
                    retry_pipeline = await self.create_pipeline(
                        app_id=pipeline.app_id,
                        params=retry_params,
                        webhook_event_id=pipeline.webhook_event_id,
                    )
                    retry_pipeline = await self.start_pipeline(
                        pipeline_id=retry_pipeline.id
                    )
                    logger.info(
                        "Auto-retrying cancelled build",
                        original_pipeline_id=str(pipeline_id),
                        retry_pipeline_id=str(retry_pipeline.id),
                        flat_manager_repo=pipeline.flat_manager_repo,
                    )
                    return pipeline, updates
                except Exception as e:
                    logger.error(
                        "Failed to auto-retry cancelled build",
                        pipeline_id=str(pipeline_id),
                        error=str(e),
                    )
            if (
                status_value == "success"
                and pipeline.params.get("workflow_id", "build.yml") == "build.yml"
            ):
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
                                    await (
                                        github_notifier.notify_flat_manager_job_status(
                                            pipeline,
                                            "commit",
                                            commit_job_id,
                                            "pending",
                                            "Committing build...",
                                        )
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
            elif pipeline.params.get("workflow_id", "build.yml") == "build.yml":
                github_notifier = GitHubNotifier()
                await github_notifier.handle_build_completion(
                    pipeline, status_value, flat_manager_client=None
                )

            updates["pipeline_status"] = status_value
            return pipeline, updates

    async def handle_reprocheck_callback(
        self,
        pipeline_id: uuid.UUID,
        callback_data: dict[str, Any],
    ) -> tuple[Pipeline, dict[str, Any]]:
        from app.services.callback import ReprochecKCallbackValidator

        validator = ReprochecKCallbackValidator()
        parsed_data = validator.validate_and_parse(callback_data)
        assert parsed_data.status is not None

        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            if pipeline.status in [
                PipelineStatus.SUCCEEDED,
                PipelineStatus.PUBLISHED,
            ]:
                raise ValueError("Pipeline status already finalized")

            status_value = parsed_data.status.lower()
            if status_value not in ["success", "failure", "cancelled"]:
                raise ValueError("status must be 'success', 'failure', or 'cancelled'")

            updates: dict[str, Any] = {}

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

            logger.info(
                "Processing reprocheck callback",
                pipeline_id=str(pipeline_id),
                has_build_pipeline_id=hasattr(parsed_data, "build_pipeline_id"),
                build_pipeline_id=getattr(parsed_data, "build_pipeline_id", None),
            )

            if (
                hasattr(parsed_data, "build_pipeline_id")
                and parsed_data.build_pipeline_id
            ):
                try:
                    build_pipeline_id = uuid.UUID(parsed_data.build_pipeline_id)
                    original_pipeline = await db.get(Pipeline, build_pipeline_id)
                    if original_pipeline and not original_pipeline.repro_pipeline_id:
                        original_pipeline.repro_pipeline_id = pipeline.id
                        flag_modified(original_pipeline, "repro_pipeline_id")
                        await db.commit()
                        logger.info(
                            "Updated original pipeline with reprocheck pipeline ID",
                            original_pipeline_id=str(build_pipeline_id),
                            reprocheck_pipeline_id=str(pipeline.id),
                        )
                except (ValueError, TypeError) as e:
                    logger.error(
                        "Invalid build_pipeline_id in reprocheck callback",
                        build_pipeline_id=parsed_data.build_pipeline_id,
                        error=str(e),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to update original pipeline with reprocheck ID",
                        build_pipeline_id=parsed_data.build_pipeline_id,
                        reprocheck_pipeline_id=str(pipeline.id),
                        error=str(e),
                    )

            updates["pipeline_status"] = status_value
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

    async def handle_publication(self, pipeline: Pipeline) -> None:
        """Handle all post-publication actions for a pipeline."""
        if pipeline.flat_manager_repo == "stable" and not pipeline.repro_pipeline_id:
            await self._dispatch_reprocheck_workflow(pipeline)

    async def _dispatch_reprocheck_workflow(self, pipeline: Pipeline) -> None:
        """Dispatch reprocheck workflow for a published stable build."""
        try:
            reprocheck_params = {
                "build_pipeline_id": str(pipeline.id),
                "owner": "flathub-infra",
                "repo": "vorarbeiter",
                "workflow_id": "reprocheck.yml",
                "ref": "main",
            }

            reprocheck_pipeline = await self.create_pipeline(
                app_id=pipeline.app_id,
                params=reprocheck_params,
            )

            reprocheck_pipeline = await self.start_pipeline(reprocheck_pipeline.id)

            logger.info(
                "Reprocheck workflow dispatched after update-repo completion",
                pipeline_id=str(pipeline.id),
                reprocheck_pipeline_id=str(reprocheck_pipeline.id),
                update_repo_job_id=pipeline.update_repo_job_id,
            )

        except Exception as e:
            logger.error(
                "Failed to dispatch reprocheck workflow",
                pipeline_id=str(pipeline.id),
                update_repo_job_id=pipeline.update_repo_job_id,
                error=str(e),
            )
