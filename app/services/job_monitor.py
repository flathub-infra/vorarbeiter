import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus
from app.services.github_actions import GitHubActionsService
from app.utils.flat_manager import (
    JobKind,
    JobResponse,
    JobStatus,
    get_flat_manager_client,
)

logger = structlog.get_logger(__name__)

JOB_DESCRIPTIONS = {
    "started": {
        "commit": "Committing build...",
        "publish": "Publishing build...",
        "update-repo": "Updating repository...",
    },
    "queued": {
        "commit": "Commit job queued",
        "publish": "Publish job queued",
        "update-repo": "Update-repo job queued",
    },
    "success": {
        "commit": "Build committed",
        "publish": "Build published",
        "update-repo": "Repository updated",
    },
    "failure": {
        "commit": "Commit failed",
        "publish": "Publish failed",
        "update-repo": "Repository update failed",
    },
}

DEFAULT_BUILD_TIMEOUT = timedelta(hours=6)
EXTENDED_BUILD_TIMEOUT = timedelta(hours=9)
BUILD_TIMEOUT_SAFETY_MARGIN = timedelta(minutes=15)


class JobMonitor:
    def __init__(self, db: AsyncSession | None = None):
        self.flat_manager = get_flat_manager_client()
        self.github_actions = GitHubActionsService()
        self.db = db
        self._db_lock = asyncio.Lock()

    async def check_all_active_pipelines(self, db: AsyncSession) -> dict[str, int]:
        now = datetime.now(tz=timezone.utc)
        cutoff_date = now - timedelta(hours=24)
        running_build_cutoff = now - DEFAULT_BUILD_TIMEOUT - BUILD_TIMEOUT_SAFETY_MARGIN

        query = select(Pipeline).where(
            or_(
                (Pipeline.created_at > cutoff_date)
                & or_(
                    (Pipeline.status == PipelineStatus.SUCCEEDED)
                    & Pipeline.commit_job_id.isnot(None),
                    (Pipeline.status == PipelineStatus.COMMITTED)
                    & Pipeline.publish_job_id.isnot(None),
                    (Pipeline.status == PipelineStatus.PUBLISHING)
                    & Pipeline.update_repo_job_id.isnot(None),
                    (Pipeline.status == PipelineStatus.SUCCEEDED)
                    & Pipeline.build_id.isnot(None)
                    & Pipeline.commit_job_id.is_(None),
                    (Pipeline.status == PipelineStatus.COMMITTED)
                    & Pipeline.build_id.isnot(None)
                    & Pipeline.publish_job_id.is_(None),
                    (Pipeline.status == PipelineStatus.PUBLISHED)
                    & (Pipeline.published_at > cutoff_date)
                    & (
                        Pipeline.publish_job_id.isnot(None)
                        | Pipeline.update_repo_job_id.isnot(None)
                    ),
                ),
                (Pipeline.status == PipelineStatus.PUBLISHING)
                & Pipeline.update_repo_job_id.is_(None)
                & Pipeline.flat_manager_repo.in_(["stable", "beta"]),
                (Pipeline.status == PipelineStatus.RUNNING)
                & (
                    (
                        Pipeline.started_at.isnot(None)
                        & (Pipeline.started_at < running_build_cutoff)
                    )
                    | (
                        Pipeline.started_at.is_(None)
                        & (Pipeline.created_at < running_build_cutoff)
                    )
                ),
            )
        )
        result = await db.execute(query)
        pipelines = list(result.scalars().all())

        # Benchmarks show the heavy /extended endpoint knees around C=5.
        sem = asyncio.Semaphore(5)

        async def _process(pipeline: Pipeline) -> bool:
            async with sem:
                try:
                    return await self.check_and_update_pipeline_jobs(pipeline)
                except Exception as e:
                    logger.warning(
                        "Error processing pipeline in check-jobs",
                        pipeline_id=str(pipeline.id),
                        error=str(e),
                    )
                    return False

        results = await asyncio.gather(*(_process(pipeline) for pipeline in pipelines))
        updated_count = sum(1 for result in results if result)

        if any(
            result
            and pipeline.status == PipelineStatus.CANCELLED
            and self._is_spot_build_type((pipeline.params or {}).get("build_type"))
            for pipeline, result in zip(pipelines, results, strict=True)
        ):
            await db.commit()
            from app.pipelines.build import BuildPipeline

            await BuildPipeline().start_pending_builds()

        return {
            "checked_pipelines": len(pipelines),
            "updated_pipelines": updated_count,
        }

    async def check_and_update_pipeline_jobs(self, pipeline: Pipeline) -> bool:
        updated = False

        if pipeline.status == PipelineStatus.RUNNING:
            return await self._expire_timed_out_running_build(pipeline)

        if pipeline.build_id and (
            pipeline.commit_job_id is None or pipeline.publish_job_id is None
        ):
            if await self._fetch_missing_job_ids(pipeline):
                updated = True

        if pipeline.status == PipelineStatus.SUCCEEDED and pipeline.commit_job_id:
            if await self._process_succeeded_pipeline(pipeline):
                updated = True
        elif (
            pipeline.status == PipelineStatus.COMMITTED
            and pipeline.publish_job_id
            and pipeline.flat_manager_repo in ["beta", "stable"]
        ):
            if await self._process_publish_job(pipeline):
                updated = True
        elif (
            pipeline.status == PipelineStatus.PUBLISHING
            and pipeline.update_repo_job_id
            and pipeline.flat_manager_repo in ["beta", "stable"]
        ):
            if await self._process_update_repo_job(pipeline):
                updated = True
        elif (
            pipeline.status == PipelineStatus.PUBLISHING
            and pipeline.update_repo_job_id is None
            and pipeline.flat_manager_repo in ["beta", "stable"]
        ):
            if await self._attempt_update_repo_recovery(pipeline):
                updated = True
            elif pipeline.created_at and pipeline.created_at < datetime.now(
                tz=timezone.utc
            ) - timedelta(hours=48):
                pipeline.status = PipelineStatus.FAILED
                logger.warning(
                    "Recovery window expired, marking pipeline as FAILED",
                    pipeline_id=str(pipeline.id),
                )
                try:
                    from app.services.github_notifier import GitHubNotifier

                    github_notifier = GitHubNotifier()
                    await github_notifier.notify_build_status(pipeline, "failure")
                except Exception as e:
                    logger.error(
                        "Failed to send recovery expiry notification",
                        pipeline_id=str(pipeline.id),
                        error=str(e),
                    )
                updated = True
        elif pipeline.status == PipelineStatus.PUBLISHED:
            if await self._check_published_pipeline_jobs(pipeline):
                updated = True

        return updated

    async def _expire_timed_out_running_build(self, pipeline: Pipeline) -> bool:
        params = pipeline.params or {}
        if params.get("workflow_id", "build.yml") != "build.yml":
            return False

        build_type = params.get("build_type") or "default"
        timeout = (
            DEFAULT_BUILD_TIMEOUT if build_type == "default" else EXTENDED_BUILD_TIMEOUT
        )
        timeout += BUILD_TIMEOUT_SAFETY_MARGIN

        started_at = await self._get_running_build_job_started_at(pipeline)
        if started_at is None:
            return False

        now = datetime.now(tz=timezone.utc)
        if started_at + timeout > now:
            return False

        pipeline.status = PipelineStatus.CANCELLED
        pipeline.finished_at = now
        logger.warning(
            "Marked timed-out running build as CANCELLED",
            pipeline_id=str(pipeline.id),
            build_type=build_type,
            started_at=started_at.isoformat(),
            timeout_minutes=int(timeout.total_seconds() // 60),
        )
        return True

    async def _get_running_build_job_started_at(
        self, pipeline: Pipeline
    ) -> datetime | None:
        run_info = self._get_github_run_info(pipeline)
        if run_info is None:
            return None

        owner, repo, run_id = run_info
        jobs = await self.github_actions.get_workflow_run_jobs(owner, repo, run_id)
        if jobs is None:
            return None

        started_at_values: list[datetime] = []
        for job in jobs:
            name = job.get("name")
            started_at = job.get("started_at")
            if (
                job.get("status") == "in_progress"
                and isinstance(name, str)
                and name.startswith("build-")
                and isinstance(started_at, str)
            ):
                parsed_started_at = self._parse_github_datetime(started_at)
                if parsed_started_at is not None:
                    started_at_values.append(parsed_started_at)

        return min(
            started_at_values,
            default=None,
        )

    @staticmethod
    def _get_github_run_info(pipeline: Pipeline) -> tuple[str, str, int] | None:
        provider_data = pipeline.provider_data or {}
        owner = provider_data.get("owner")
        repo = provider_data.get("repo")
        run_id = provider_data.get("run_id")

        if pipeline.log_url:
            parsed = urlparse(pipeline.log_url)
            path_parts = parsed.path.strip("/").split("/")
            if (
                len(path_parts) >= 5
                and path_parts[2] == "actions"
                and path_parts[3] == "runs"
            ):
                if not isinstance(owner, str):
                    owner = path_parts[0]
                if not isinstance(repo, str):
                    repo = path_parts[1]
                run_id = run_id or path_parts[4]

        if not isinstance(owner, str) or not isinstance(repo, str) or run_id is None:
            return None

        try:
            run_id_int = int(run_id)
        except (TypeError, ValueError):
            return None

        return owner, repo, run_id_int

    @staticmethod
    def _parse_github_datetime(value: str | None) -> datetime | None:
        if not value:
            return None

        try:
            if value.endswith("Z"):
                value = value.removesuffix("Z") + "+00:00"
            return JobMonitor._as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None

    @staticmethod
    def _is_spot_build_type(build_type: str | None) -> bool:
        from app.pipelines.build import BuildPipeline

        return BuildPipeline.is_spot_build_type(build_type)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _process_succeeded_pipeline(self, pipeline: Pipeline) -> bool:
        if not pipeline.commit_job_id:
            return False

        try:
            job_response = await self.flat_manager.get_job(pipeline.commit_job_id)
            job_status = JobStatus(job_response["status"])

            if job_status == JobStatus.ENDED:
                pipeline.status = PipelineStatus.COMMITTED
                logger.info(
                    "Pipeline transitioned to COMMITTED",
                    pipeline_id=str(pipeline.id),
                    commit_job_id=pipeline.commit_job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, "commit", pipeline.commit_job_id, success=True
                )
                await self._notify_committed(pipeline)
                return True
            elif job_status == JobStatus.BROKEN:
                pipeline.status = PipelineStatus.FAILED
                logger.warning(
                    "Commit job failed, marking pipeline as FAILED",
                    pipeline_id=str(pipeline.id),
                    commit_job_id=pipeline.commit_job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, "commit", pipeline.commit_job_id, success=False
                )
                await self._create_job_failure_issue(
                    pipeline, "commit", pipeline.commit_job_id, job_response
                )
                if pipeline.params.get("pr_number"):
                    try:
                        from app.services.github_notifier import GitHubNotifier

                        github_notifier = GitHubNotifier()
                        await github_notifier.notify_build_status(pipeline, "failure")
                        await github_notifier.notify_pr_build_complete(
                            pipeline, "commit_failure"
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to send commit failure notification",
                            pipeline_id=str(pipeline.id),
                            error=str(e),
                        )
                return True
            else:
                logger.debug(
                    "Commit job still in progress",
                    pipeline_id=str(pipeline.id),
                    commit_job_id=pipeline.commit_job_id,
                    job_status=job_status.name,
                )
                return False

        except Exception as e:
            logger.warning(
                "Failed to check commit job status",
                pipeline_id=str(pipeline.id),
                commit_job_id=pipeline.commit_job_id,
                error=str(e),
            )
            return False

    async def check_commit_job_status(self, job_id: int) -> JobStatus | None:
        try:
            job_response = await self.flat_manager.get_job(job_id)
            return JobStatus(job_response["status"])
        except Exception as e:
            logger.warning(
                "Failed to check job status",
                job_id=job_id,
                error=str(e),
            )
            return None

    async def _fetch_and_validate_job(
        self,
        pipeline: Pipeline,
        job_id: int,
        expected_kind: JobKind,
        job_type: str,
        job_id_field_name: str,
    ) -> tuple[JobStatus, JobResponse] | None:
        try:
            job_response = await self.flat_manager.get_job(job_id)
            job_status = JobStatus(job_response["status"])
            job_kind = JobKind(job_response["kind"])

            if job_kind != expected_kind:
                logger.warning(
                    f"Job is not a {job_type} job",
                    pipeline_id=str(pipeline.id),
                    job_id=job_id,
                    job_kind=job_kind.name,
                )
                return None

            return (job_status, job_response)
        except Exception as e:
            logger.warning(
                f"Failed to check {job_type} job status",
                pipeline_id=str(pipeline.id),
                **{job_id_field_name: job_id},
                error=str(e),
            )
            return None

    async def _handle_broken_flat_manager_job(
        self,
        pipeline: Pipeline,
        job_type: str,
        job_id: int,
        job_response: JobResponse,
        create_failure_issue: bool = True,
    ) -> None:
        pipeline.status = PipelineStatus.FAILED
        logger.warning(
            f"{job_type.capitalize()} job failed, marking pipeline as FAILED",
            pipeline_id=str(pipeline.id),
            job_id=job_id,
        )
        await self._notify_flat_manager_job_completed(
            pipeline, job_type, job_id, success=False
        )
        if create_failure_issue:
            await self._create_job_failure_issue(
                pipeline, job_type, job_id, job_response
            )

    async def _process_publish_job(self, pipeline: Pipeline) -> bool:
        if not pipeline.publish_job_id:
            return False

        result = await self._fetch_and_validate_job(
            pipeline,
            pipeline.publish_job_id,
            JobKind.PUBLISH,
            "publish",
            "publish_job_id",
        )
        if result is None:
            return False

        job_status, job_response = result

        if job_status == JobStatus.ENDED:
            results = job_response.get("results")
            if results:
                try:
                    results_data = json.loads(results)
                    update_repo_job_id = results_data.get("update-repo-job")
                    if update_repo_job_id:
                        pipeline.update_repo_job_id = update_repo_job_id
                        pipeline.status = PipelineStatus.PUBLISHING
                        logger.info(
                            "Extracted update-repo job ID from publish job results, transitioning to PUBLISHING",
                            pipeline_id=str(pipeline.id),
                            publish_job_id=pipeline.publish_job_id,
                            update_repo_job_id=update_repo_job_id,
                        )
                        await self._notify_flat_manager_job_completed(
                            pipeline,
                            "publish",
                            pipeline.publish_job_id,
                            success=True,
                        )
                        await self._notify_flat_manager_job_started(
                            pipeline, "update-repo", update_repo_job_id
                        )
                        return True
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(
                        "Failed to parse publish job results",
                        pipeline_id=str(pipeline.id),
                        publish_job_id=pipeline.publish_job_id,
                        error=str(e),
                    )
            logger.warning(
                "Publish job completed but no update-repo job ID found",
                pipeline_id=str(pipeline.id),
                publish_job_id=pipeline.publish_job_id,
            )
            return False
        elif job_status == JobStatus.BROKEN:
            await self._handle_broken_flat_manager_job(
                pipeline, "publish", pipeline.publish_job_id, job_response
            )
            return True
        else:
            logger.debug(
                "Publish job still in progress",
                pipeline_id=str(pipeline.id),
                publish_job_id=pipeline.publish_job_id,
                job_status=job_status.name,
            )
            return False

    async def _process_update_repo_job(self, pipeline: Pipeline) -> bool:
        if not pipeline.update_repo_job_id:
            return False

        result = await self._fetch_and_validate_job(
            pipeline,
            pipeline.update_repo_job_id,
            JobKind.UPDATE_REPO,
            "update-repo",
            "update_repo_job_id",
        )
        if result is None:
            return False

        job_status, job_response = result

        if job_status == JobStatus.ENDED:
            pipeline.status = PipelineStatus.PUBLISHED
            pipeline.published_at = datetime.now(tz=timezone.utc)
            logger.info(
                "Update-repo job completed, pipeline published",
                pipeline_id=str(pipeline.id),
                update_repo_job_id=pipeline.update_repo_job_id,
            )
            await self._notify_flat_manager_job_completed(
                pipeline, "update-repo", pipeline.update_repo_job_id, success=True
            )

            from app.pipelines.build import BuildPipeline

            build_pipeline = BuildPipeline()
            try:
                await build_pipeline.handle_publication(pipeline)
            except Exception as e:
                logger.error(
                    "Error in post-publication handling",
                    pipeline_id=str(pipeline.id),
                    error=str(e),
                )

            return True
        elif job_status == JobStatus.BROKEN:
            logger.warning(
                "Update-repo job failed, clearing job ID to attempt recovery via peer pipeline",
                pipeline_id=str(pipeline.id),
                update_repo_job_id=pipeline.update_repo_job_id,
            )
            pipeline.update_repo_job_id = None
            return True
        else:
            logger.debug(
                "Update-repo job still in progress",
                pipeline_id=str(pipeline.id),
                update_repo_job_id=pipeline.update_repo_job_id,
                job_status=job_status.name,
            )
            return False

    async def _attempt_update_repo_recovery(self, pipeline: Pipeline) -> bool:
        if not self.db:
            return False

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=48)
        query = (
            select(Pipeline)
            .where(
                Pipeline.id != pipeline.id,
                Pipeline.status == PipelineStatus.PUBLISHED,
                Pipeline.flat_manager_repo == pipeline.flat_manager_repo,
                Pipeline.published_at > cutoff,
                Pipeline.published_at > pipeline.created_at,
                Pipeline.update_repo_job_id.isnot(None),
            )
            .order_by(Pipeline.published_at.desc())
        )
        async with self._db_lock:
            result = await self.db.execute(query)
            peer = result.scalars().first()

        if not peer or not peer.update_repo_job_id:
            return False

        pipeline.update_repo_job_id = peer.update_repo_job_id
        pipeline.status = PipelineStatus.PUBLISHED
        pipeline.published_at = datetime.now(tz=timezone.utc)
        logger.info(
            "Recovered pipeline via peer update-repo success",
            pipeline_id=str(pipeline.id),
            peer_pipeline_id=str(peer.id),
            peer_update_repo_job_id=peer.update_repo_job_id,
        )

        await self._notify_flat_manager_job_completed(
            pipeline, "update-repo", peer.update_repo_job_id, success=True
        )

        from app.pipelines.build import BuildPipeline

        build_pipeline = BuildPipeline()
        try:
            await build_pipeline.handle_publication(pipeline)
        except Exception as e:
            logger.error(
                "Error in post-publication handling during recovery",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )

        return True

    async def _fetch_missing_job_ids(self, pipeline: Pipeline) -> bool:
        if not pipeline.build_id:
            return False

        try:
            build_info = await self.flat_manager.get_build_info(pipeline.build_id)
            build_data = build_info.get("build", {})

            commit_job_id = build_data.get("commit_job_id")
            publish_job_id = build_data.get("publish_job_id")

            updated = False
            if commit_job_id is not None and pipeline.commit_job_id is None:
                pipeline.commit_job_id = commit_job_id
                updated = True
                logger.info(
                    "Fetched commit_job_id from flat-manager",
                    pipeline_id=str(pipeline.id),
                    commit_job_id=commit_job_id,
                )
                await self._check_and_notify_new_job(pipeline, "commit", commit_job_id)

            if (
                pipeline.flat_manager_repo in ["beta", "stable"]
                and publish_job_id is not None
                and pipeline.publish_job_id is None
            ):
                pipeline.publish_job_id = publish_job_id
                updated = True
                logger.info(
                    "Fetched publish_job_id from flat-manager",
                    pipeline_id=str(pipeline.id),
                    publish_job_id=publish_job_id,
                )
                await self._check_and_notify_new_job(
                    pipeline, "publish", publish_job_id
                )

            return updated
        except Exception as e:
            logger.warning(
                "Failed to fetch job IDs from flat-manager",
                pipeline_id=str(pipeline.id),
                build_id=pipeline.build_id,
                error=str(e),
            )
            return False

    async def _notify_committed(self, pipeline: Pipeline) -> None:
        try:
            from app.services.github_notifier import GitHubNotifier

            flat_manager = None
            if pipeline.params.get("pr_number"):
                flat_manager = get_flat_manager_client()

            github_notifier = GitHubNotifier(flat_manager_client=flat_manager)
            await github_notifier.handle_build_committed(
                pipeline, flat_manager_client=flat_manager
            )
        except Exception as e:
            logger.error(
                "Failed to send committed notification",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )

    async def _notify_flat_manager_job(
        self,
        pipeline: Pipeline,
        job_type: str,
        job_id: int,
        notification_type: str,
        state: str,
        fallback_description: str,
        error_action: str,
    ) -> None:
        if pipeline.flat_manager_repo not in ["stable", "beta"]:
            return

        try:
            from app.services.github_notifier import GitHubNotifier

            github_notifier = GitHubNotifier()
            description = JOB_DESCRIPTIONS.get(notification_type, {}).get(
                job_type, fallback_description
            )

            await github_notifier.notify_flat_manager_job_status(
                pipeline, job_type, job_id, state, description
            )
        except Exception as e:
            logger.error(
                f"Failed to notify {job_type} job {error_action}",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
            )

    async def _notify_flat_manager_job_started(
        self, pipeline: Pipeline, job_type: str, job_id: int
    ) -> None:
        await self._notify_flat_manager_job(
            pipeline,
            job_type,
            job_id,
            notification_type="started",
            state="pending",
            fallback_description=f"{job_type} in progress...",
            error_action="started",
        )

    async def _check_and_notify_new_job(
        self, pipeline: Pipeline, job_type: str, job_id: int
    ) -> None:
        if pipeline.flat_manager_repo not in ["stable", "beta"]:
            return

        try:
            job_response = await self.flat_manager.get_job(job_id)
            job_status = JobStatus(job_response["status"])

            if job_status == JobStatus.NEW:
                await self._notify_flat_manager_job_new(pipeline, job_type, job_id)
        except Exception as e:
            logger.warning(
                f"Failed to check {job_type} job status for NEW notification",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
            )

    async def _notify_flat_manager_job_new(
        self, pipeline: Pipeline, job_type: str, job_id: int
    ) -> None:
        await self._notify_flat_manager_job(
            pipeline,
            job_type,
            job_id,
            notification_type="queued",
            state="pending",
            fallback_description=f"{job_type} job queued",
            error_action="queued",
        )

    async def _report_published_job_status(
        self, pipeline: Pipeline, job_id: int, job_type: str
    ) -> bool:
        """Report job status for a PUBLISHED pipeline.

        Fetches current job status and notifies if completed or failed.
        Returns True if a notification was sent, False otherwise.
        """
        try:
            job_response = await self.flat_manager.get_job(job_id)
            job_status = JobStatus(job_response["status"])

            if job_status == JobStatus.ENDED:
                logger.info(
                    f"Reporting completed {job_type} job status for published pipeline",
                    pipeline_id=str(pipeline.id),
                    job_id=job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, job_type, job_id, success=True
                )
                return True
            elif job_status == JobStatus.BROKEN:
                logger.info(
                    f"Reporting failed {job_type} job status for published pipeline",
                    pipeline_id=str(pipeline.id),
                    job_id=job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, job_type, job_id, success=False
                )
                return True
        except Exception as e:
            logger.warning(
                f"Failed to check {job_type} job status for published pipeline",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
            )
        return False

    async def _check_published_pipeline_jobs(self, pipeline: Pipeline) -> bool:
        """Check and report any unreported job statuses for PUBLISHED pipelines."""
        updated = False

        if pipeline.publish_job_id and pipeline.flat_manager_repo in ["beta", "stable"]:
            if await self._report_published_job_status(
                pipeline, pipeline.publish_job_id, "publish"
            ):
                updated = True

        if pipeline.update_repo_job_id and pipeline.flat_manager_repo in [
            "beta",
            "stable",
        ]:
            if await self._report_published_job_status(
                pipeline, pipeline.update_repo_job_id, "update-repo"
            ):
                updated = True

        return updated

    async def _notify_flat_manager_job_completed(
        self, pipeline: Pipeline, job_type: str, job_id: int, success: bool
    ) -> None:
        if success:
            notification_type = "success"
            state = "success"
            fallback = f"{job_type} completed"
        else:
            notification_type = "failure"
            state = "failure"
            fallback = f"{job_type} failed"

        await self._notify_flat_manager_job(
            pipeline,
            job_type,
            job_id,
            notification_type=notification_type,
            state=state,
            fallback_description=fallback,
            error_action="completion",
        )

    async def _create_job_failure_issue(
        self, pipeline: Pipeline, job_type: str, job_id: int, job_response: JobResponse
    ) -> None:
        try:
            from app.services.github_notifier import GitHubNotifier

            github_notifier = GitHubNotifier()
            await github_notifier.create_stable_job_failure_issue(
                pipeline, job_type, job_id, dict(job_response)
            )
        except Exception as e:
            logger.error(
                f"Failed to create GitHub issue for {job_type} job failure",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
            )
