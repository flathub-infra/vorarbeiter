import secrets
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

import httpx
import sentry_sdk
import structlog
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.services import github_actions_service
from app.services.github_actions import GitHubActionsService
from app.services.github_notifier import GitHubNotifier
from app.utils.flat_manager import (
    FlatManagerClient,
    get_flat_manager_client,
    get_flat_manager_repo,
)

logger = structlog.get_logger(__name__)


class CallbackData(BaseModel):
    status: Optional[str] = None
    log_url: Optional[str] = None
    app_id: Optional[str] = None
    is_extra_data: Optional[bool] = None
    end_of_life: Optional[str] = None
    end_of_life_rebase: Optional[str] = None
    build_pipeline_id: Optional[str] = None
    status_code: Optional[str] = None
    timestamp: Optional[str] = None
    result_url: Optional[str] = None
    message: Optional[str] = None
    cost: Optional[float] = None


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
    "io.qt.qtwebengine.BaseApp": "large",
    "com.collaboraoffice.Office": "large",
}

app_medium_builds: set[str] = set()

FAST_BUILD_P90_THRESHOLD_MINUTES = 15.0
FAST_BUILD_MIN_BUILDS = 3
FAST_BUILD_LOOKBACK_DAYS = 90


async def get_app_p90_build_time(db: AsyncSession, app_id: str) -> float | None:
    query = text("""
        SELECT PERCENTILE_CONT(0.9) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))/60
        ) as p90_min
        FROM pipeline
        WHERE app_id = :app_id
          AND status = 'published'
          AND finished_at IS NOT NULL
          AND started_at IS NOT NULL
          AND (params->>'build_type' IS NULL OR params->>'build_type' != 'large')
          AND started_at > NOW() - INTERVAL '1 day' * :lookback_days
        HAVING COUNT(*) >= :min_builds
    """)
    result = await db.execute(
        query,
        {
            "app_id": app_id,
            "min_builds": FAST_BUILD_MIN_BUILDS,
            "lookback_days": FAST_BUILD_LOOKBACK_DAYS,
        },
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return float(row[0])


async def determine_build_type(db: AsyncSession, app_id: str) -> str:
    if app_id in app_build_types:
        return app_build_types[app_id]

    if app_id in app_medium_builds:
        return "medium"

    p90 = await get_app_p90_build_time(db, app_id)
    if p90 is not None and p90 <= FAST_BUILD_P90_THRESHOLD_MINUTES:
        return "default"

    return "medium"


async def _validate_and_prepare_callback(
    validator_class: type,
    callback_data: dict[str, Any],
    pipeline_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[Pipeline, CallbackData, str]:
    validator = validator_class()
    parsed_data = validator.validate_and_parse(callback_data)
    assert parsed_data.status is not None

    pipeline = await db.get(Pipeline, pipeline_id)
    if not pipeline:
        raise ValueError(f"Pipeline {pipeline_id} not found")

    if pipeline.status in [PipelineStatus.SUCCEEDED, PipelineStatus.PUBLISHED]:
        raise ValueError("Pipeline status already finalized")

    status_value = parsed_data.status.lower()
    if status_value not in ["success", "failure", "cancelled"]:
        raise ValueError("status must be 'success', 'failure', or 'cancelled'")

    return pipeline, parsed_data, status_value


class BuildPipeline:
    def __init__(self):
        self.provider = github_actions_service
        self.flat_manager = get_flat_manager_client()

    @staticmethod
    async def fetch_and_store_job_id(
        pipeline: Pipeline,
        flat_manager: FlatManagerClient,
        job_type: Literal["commit", "publish"],
        github_notifier: GitHubNotifier | None = None,
        db=None,
    ) -> bool:
        """
        Fetch and store a job ID from flat-manager build info.

        Args:
            pipeline: Pipeline to update
            flat_manager: FlatManagerClient instance
            job_type: Type of job ("commit" or "publish")
            github_notifier: Optional GitHubNotifier for status updates
            db: Optional AsyncSession for immediate commit

        Returns:
            True if job ID was stored, False otherwise
        """
        if not pipeline.build_id:
            return False

        job_id_field = f"{job_type}_job_id"
        status_message = (
            "Committing build..." if job_type == "commit" else "Publishing build..."
        )

        try:
            build_info = await flat_manager.get_build_info(pipeline.build_id)
            build_data = build_info.get("build", {})
            job_id = build_data.get(job_id_field)

            if job_id and not getattr(pipeline, job_id_field):
                setattr(pipeline, job_id_field, job_id)
                logger.info(
                    f"Stored {job_type} job ID",
                    **{job_id_field: job_id, "pipeline_id": str(pipeline.id)},
                )

                if db:
                    await db.commit()

                if github_notifier and pipeline.flat_manager_repo in ["stable", "beta"]:
                    await github_notifier.notify_flat_manager_job_status(
                        pipeline, job_type, job_id, "pending", status_message
                    )

                return True
        except Exception as e:
            logger.warning(
                f"Failed to fetch {job_type} job ID",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )

        return False

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

    async def _supersede_conflicting_pipelines(
        self,
        db: AsyncSession,
        pipeline: Pipeline,
        flat_manager_repo: str,
    ) -> None:
        if flat_manager_repo not in ["stable", "beta"]:
            return

        query = select(Pipeline).where(
            Pipeline.app_id == pipeline.app_id,
            Pipeline.flat_manager_repo == flat_manager_repo,
            Pipeline.status.in_([PipelineStatus.RUNNING, PipelineStatus.PENDING]),
            Pipeline.id != pipeline.id,
        )
        result = await db.execute(query)
        conflicting = list(result.scalars().all())

        for old_pipeline in conflicting:
            old_pipeline.status = PipelineStatus.SUPERSEDED
            logger.info(
                "Superseded conflicting pipeline at start time",
                superseded_pipeline_id=str(old_pipeline.id),
                new_pipeline_id=str(pipeline.id),
                app_id=pipeline.app_id,
                flat_manager_repo=flat_manager_repo,
            )

            if old_pipeline.build_id:
                try:
                    await self.flat_manager.purge(old_pipeline.build_id)
                    logger.info(
                        "Purged build for superseded pipeline",
                        build_id=old_pipeline.build_id,
                        pipeline_id=str(old_pipeline.id),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to purge build for superseded pipeline",
                        build_id=old_pipeline.build_id,
                        pipeline_id=str(old_pipeline.id),
                        error=str(e),
                    )

            run_id = (old_pipeline.provider_data or {}).get("run_id")
            if run_id:
                try:
                    github_actions = GitHubActionsService()
                    await github_actions.cancel(
                        str(old_pipeline.id), old_pipeline.provider_data
                    )
                    logger.info(
                        "Cancelled GitHub Actions run for superseded pipeline",
                        run_id=run_id,
                        pipeline_id=str(old_pipeline.id),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to cancel GitHub Actions run",
                        run_id=run_id,
                        pipeline_id=str(old_pipeline.id),
                        error=str(e),
                    )

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

            build_type = await determine_build_type(db, pipeline.app_id)
            pipeline.params = {**pipeline.params, "build_type": build_type}

            ref = pipeline.params.get("ref")
            flat_manager_repo = get_flat_manager_repo(ref)
            pipeline.flat_manager_repo = flat_manager_repo

            await self._supersede_conflicting_pipelines(db, pipeline, flat_manager_repo)

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
                provider_data = dict(pipeline.provider_data or {})
                provider_data["run_id"] = run_id
                pipeline.provider_data = provider_data
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

        async with get_db() as db:
            pipeline, parsed_data, status_value = await _validate_and_prepare_callback(
                StatusCallbackValidator, callback_data, pipeline_id, db
            )

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
                    flat_manager = get_flat_manager_client()
                github_notifier = GitHubNotifier(flat_manager_client=flat_manager)
                await github_notifier.handle_build_completion(
                    pipeline, status_value, flat_manager_client=flat_manager
                )
                if pipeline.build_id:
                    try:
                        if not flat_manager:
                            flat_manager = get_flat_manager_client()
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

                        await BuildPipeline.fetch_and_store_job_id(
                            pipeline, flat_manager, "commit", github_notifier, db
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
        from app.services.callback import ReprocheckCallbackValidator

        async with get_db() as db:
            pipeline, parsed_data, status_value = await _validate_and_prepare_callback(
                ReprocheckCallbackValidator, callback_data, pipeline_id, db
            )

            reprocheck_result = {}
            if parsed_data.status_code is not None:
                reprocheck_result["status_code"] = parsed_data.status_code
            if parsed_data.timestamp is not None:
                reprocheck_result["timestamp"] = parsed_data.timestamp
            if parsed_data.result_url is not None:
                reprocheck_result["result_url"] = parsed_data.result_url
            if parsed_data.message is not None:
                reprocheck_result["message"] = parsed_data.message

            if reprocheck_result:
                pipeline.params = {
                    **pipeline.params,
                    "reprocheck_result": reprocheck_result,
                }

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

            await db.flush()

            build_pipeline_id_value = getattr(parsed_data, "build_pipeline_id", None)
            workflow_id = (pipeline.params or {}).get("workflow_id")
            has_build_id = bool(build_pipeline_id_value)

            logger.info(
                "Processing reprocheck callback",
                pipeline_id=str(pipeline_id),
                workflow_id=workflow_id,
                build_pipeline_id=build_pipeline_id_value,
            )

            if workflow_id != "reprocheck.yml":
                logger.info(
                    "Skipping reprocheck callback for non-repro pipeline",
                    pipeline_id=str(pipeline_id),
                    workflow_id=workflow_id,
                )
            elif not has_build_id:
                logger.warning(
                    "Reprocheck callback missing build_pipeline_id",
                    pipeline_id=str(pipeline_id),
                )
            else:
                try:
                    build_pipeline_id = uuid.UUID(build_pipeline_id_value)
                    # Don't load the object into this session to avoid stale overwrites
                    check_stmt = text(
                        "SELECT id, repro_pipeline_id FROM pipeline WHERE id = :build_id"
                    )
                    result = await db.execute(
                        check_stmt,
                        {"build_id": str(build_pipeline_id)},
                    )
                    row = result.first()

                    if row:
                        original_repro_id = row[1]
                        if original_repro_id:
                            logger.info(
                                "Skipping repro_pipeline_id update - already set",
                                build_pipeline_id=str(build_pipeline_id),
                                existing_repro_pipeline_id=str(original_repro_id),
                            )
                        else:
                            # Separate session to avoid main transaction overwriting the update
                            async with get_db() as update_db:
                                stmt = text(
                                    "UPDATE pipeline SET repro_pipeline_id = :repro_id WHERE id = :build_id"
                                )
                                await update_db.execute(
                                    stmt,
                                    {
                                        "repro_id": str(pipeline.id),
                                        "build_id": str(build_pipeline_id),
                                    },
                                )
                                await update_db.commit()

                            logger.info(
                                "Updated original pipeline with reprocheck pipeline ID",
                                original_pipeline_id=str(build_pipeline_id),
                                reprocheck_pipeline_id=str(pipeline.id),
                            )
                    else:
                        logger.warning(
                            "Original pipeline not found",
                            build_pipeline_id=str(build_pipeline_id),
                            reprocheck_pipeline_id=str(pipeline.id),
                        )
                except (ValueError, TypeError) as e:
                    logger.error(
                        "Invalid build_pipeline_id in reprocheck callback",
                        build_pipeline_id=build_pipeline_id_value,
                        error=str(e),
                    )
                    try:
                        sentry_sdk.capture_exception(
                            e,
                            contexts={
                                "reprocheck": {
                                    "build_pipeline_id": build_pipeline_id_value,
                                    "reprocheck_pipeline_id": str(pipeline.id),
                                }
                            },
                        )
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(
                        "Failed to update original pipeline with reprocheck ID",
                        build_pipeline_id=build_pipeline_id_value,
                        reprocheck_pipeline_id=str(pipeline.id),
                        error=str(e),
                    )
                    try:
                        sentry_sdk.capture_exception(
                            e,
                            contexts={
                                "reprocheck": {
                                    "build_pipeline_id": build_pipeline_id_value,
                                    "reprocheck_pipeline_id": str(pipeline.id),
                                }
                            },
                        )
                    except Exception:
                        pass

            if settings.ff_reprocheck_issues:
                try:
                    from app.services.reprocheck_notification import (
                        ReprocheckNotificationService,
                    )

                    notification_service = ReprocheckNotificationService()
                    await notification_service.handle_reprocheck_result(db, pipeline)
                except Exception as e:
                    logger.warning(
                        "Failed to process reprocheck notification",
                        pipeline_id=str(pipeline.id),
                        error=str(e),
                    )

            updates["pipeline_status"] = status_value
            return pipeline, updates

    async def handle_cost_callback(
        self,
        pipeline_id: uuid.UUID,
        callback_data: dict[str, Any],
    ) -> tuple[Pipeline, dict[str, Any]]:
        from app.services.callback import CostCallbackValidator

        validator = CostCallbackValidator()
        parsed_data = validator.validate_and_parse(callback_data)
        assert parsed_data.cost is not None

        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if not pipeline:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            pipeline.total_cost = (pipeline.total_cost or 0) + parsed_data.cost

            await db.commit()

            updates: dict[str, Any] = {"total_cost": pipeline.total_cost}
            return pipeline, updates

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
