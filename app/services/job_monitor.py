import structlog
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Pipeline, PipelineStatus
from app.utils.flat_manager import FlatManagerClient, JobStatus, JobKind

logger = structlog.get_logger(__name__)


class JobMonitor:
    def __init__(self):
        self.flat_manager = FlatManagerClient(
            url=settings.flat_manager_url,
            token=settings.flat_manager_token,
        )

    async def check_and_update_pipeline_jobs(
        self, db: AsyncSession, pipeline: Pipeline
    ) -> bool:
        updated = False

        if pipeline.build_id and (
            pipeline.commit_job_id is None or pipeline.publish_job_id is None
        ):
            if await self._fetch_missing_job_ids(pipeline):
                updated = True

        if pipeline.status == PipelineStatus.SUCCEEDED and pipeline.commit_job_id:
            if await self._process_succeeded_pipeline(db, pipeline):
                updated = True
        elif (
            pipeline.status == PipelineStatus.COMMITTED
            and pipeline.publish_job_id
            and pipeline.flat_manager_repo in ["beta", "stable"]
        ):
            if await self._process_publish_job(db, pipeline):
                updated = True
        elif (
            pipeline.status == PipelineStatus.PUBLISHING
            and pipeline.update_repo_job_id
            and pipeline.flat_manager_repo in ["beta", "stable"]
        ):
            if await self._process_update_repo_job(db, pipeline):
                updated = True
        elif pipeline.status == PipelineStatus.PUBLISHED:
            if await self._check_published_pipeline_jobs(db, pipeline):
                updated = True

        return updated

    async def _process_succeeded_pipeline(
        self, db: AsyncSession, pipeline: Pipeline
    ) -> bool:
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
                return True
            elif job_status == JobStatus.BROKEN:
                pipeline.status = PipelineStatus.FAILED
                logger.error(
                    "Commit job failed, marking pipeline as FAILED",
                    pipeline_id=str(pipeline.id),
                    commit_job_id=pipeline.commit_job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, "commit", pipeline.commit_job_id, success=False
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
            logger.error(
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
            logger.error(
                "Failed to check job status",
                job_id=job_id,
                error=str(e),
            )
            return None

    async def _process_publish_job(self, db: AsyncSession, pipeline: Pipeline) -> bool:
        if not pipeline.publish_job_id:
            return False

        try:
            job_response = await self.flat_manager.get_job(pipeline.publish_job_id)
            job_status = JobStatus(job_response["status"])
            job_kind = JobKind(job_response["kind"])

            if job_kind != JobKind.PUBLISH:
                logger.warning(
                    "Job is not a publish job",
                    pipeline_id=str(pipeline.id),
                    job_id=pipeline.publish_job_id,
                    job_kind=job_kind.name,
                )
                return False

            if job_status == JobStatus.ENDED:
                results = job_response.get("results")
                if results:
                    try:
                        import json

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
                pipeline.status = PipelineStatus.FAILED
                logger.error(
                    "Publish job failed, marking pipeline as FAILED",
                    pipeline_id=str(pipeline.id),
                    publish_job_id=pipeline.publish_job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, "publish", pipeline.publish_job_id, success=False
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

        except Exception as e:
            logger.error(
                "Failed to check publish job status",
                pipeline_id=str(pipeline.id),
                publish_job_id=pipeline.publish_job_id,
                error=str(e),
            )
            return False

    async def _process_update_repo_job(
        self, db: AsyncSession, pipeline: Pipeline
    ) -> bool:
        if not pipeline.update_repo_job_id:
            return False

        try:
            job_response = await self.flat_manager.get_job(pipeline.update_repo_job_id)
            job_status = JobStatus(job_response["status"])
            job_kind = JobKind(job_response["kind"])

            if job_kind != JobKind.UPDATE_REPO:
                logger.warning(
                    "Job is not an update-repo job",
                    pipeline_id=str(pipeline.id),
                    job_id=pipeline.update_repo_job_id,
                    job_kind=job_kind.name,
                )
                return False

            if job_status == JobStatus.ENDED:
                pipeline.status = PipelineStatus.PUBLISHED
                pipeline.published_at = datetime.now()
                logger.info(
                    "Update-repo job completed, pipeline published",
                    pipeline_id=str(pipeline.id),
                    update_repo_job_id=pipeline.update_repo_job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, "update-repo", pipeline.update_repo_job_id, success=True
                )
                return True
            elif job_status == JobStatus.BROKEN:
                pipeline.status = PipelineStatus.FAILED
                logger.error(
                    "Update-repo job failed, marking pipeline as FAILED",
                    pipeline_id=str(pipeline.id),
                    update_repo_job_id=pipeline.update_repo_job_id,
                )
                await self._notify_flat_manager_job_completed(
                    pipeline, "update-repo", pipeline.update_repo_job_id, success=False
                )
                return True
            else:
                logger.debug(
                    "Update-repo job still in progress",
                    pipeline_id=str(pipeline.id),
                    update_repo_job_id=pipeline.update_repo_job_id,
                    job_status=job_status.name,
                )
                return False

        except Exception as e:
            logger.error(
                "Failed to check update-repo job status",
                pipeline_id=str(pipeline.id),
                update_repo_job_id=pipeline.update_repo_job_id,
                error=str(e),
            )
            return False

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
            logger.error(
                "Failed to fetch job IDs from flat-manager",
                pipeline_id=str(pipeline.id),
                build_id=pipeline.build_id,
                error=str(e),
            )
            return False

    async def _notify_committed(self, pipeline: Pipeline) -> None:
        try:
            from app.services.github_notifier import GitHubNotifier
            from app.utils.flat_manager import FlatManagerClient

            flat_manager = None
            if pipeline.params.get("pr_number"):
                flat_manager = FlatManagerClient(
                    url=settings.flat_manager_url,
                    token=settings.flat_manager_token,
                )

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

    async def _notify_flat_manager_job_started(
        self, pipeline: Pipeline, job_type: str, job_id: int
    ) -> None:
        if pipeline.flat_manager_repo not in ["stable", "beta"]:
            return

        try:
            from app.services.github_notifier import GitHubNotifier

            github_notifier = GitHubNotifier()
            description = {
                "commit": "Committing build...",
                "publish": "Publishing build...",
                "update-repo": "Updating repository...",
            }.get(job_type, f"{job_type} in progress...")

            await github_notifier.notify_flat_manager_job_status(
                pipeline, job_type, job_id, "pending", description
            )
        except Exception as e:
            logger.error(
                f"Failed to notify {job_type} job started",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
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
            logger.error(
                f"Failed to check {job_type} job status for NEW notification",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
            )

    async def _notify_flat_manager_job_new(
        self, pipeline: Pipeline, job_type: str, job_id: int
    ) -> None:
        if pipeline.flat_manager_repo not in ["stable", "beta"]:
            return

        try:
            from app.services.github_notifier import GitHubNotifier

            github_notifier = GitHubNotifier()
            description = {
                "commit": "Commit job queued",
                "publish": "Publish job queued",
                "update-repo": "Update-repo job queued",
            }.get(job_type, f"{job_type} job queued")

            await github_notifier.notify_flat_manager_job_status(
                pipeline, job_type, job_id, "pending", description
            )
        except Exception as e:
            logger.error(
                f"Failed to notify {job_type} job queued",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                error=str(e),
            )

    async def _check_published_pipeline_jobs(
        self, db: AsyncSession, pipeline: Pipeline
    ) -> bool:
        """Check and report any unreported job statuses for PUBLISHED pipelines."""
        updated = False

        if pipeline.publish_job_id and pipeline.flat_manager_repo in ["beta", "stable"]:
            try:
                job_response = await self.flat_manager.get_job(pipeline.publish_job_id)
                job_status = JobStatus(job_response["status"])

                if job_status == JobStatus.ENDED:
                    logger.info(
                        "Reporting completed publish job status for published pipeline",
                        pipeline_id=str(pipeline.id),
                        publish_job_id=pipeline.publish_job_id,
                    )
                    await self._notify_flat_manager_job_completed(
                        pipeline, "publish", pipeline.publish_job_id, success=True
                    )
                    updated = True
                elif job_status == JobStatus.BROKEN:
                    logger.info(
                        "Reporting failed publish job status for published pipeline",
                        pipeline_id=str(pipeline.id),
                        publish_job_id=pipeline.publish_job_id,
                    )
                    await self._notify_flat_manager_job_completed(
                        pipeline, "publish", pipeline.publish_job_id, success=False
                    )
                    updated = True
            except Exception as e:
                logger.error(
                    "Failed to check publish job status for published pipeline",
                    pipeline_id=str(pipeline.id),
                    publish_job_id=pipeline.publish_job_id,
                    error=str(e),
                )

        if pipeline.update_repo_job_id and pipeline.flat_manager_repo in [
            "beta",
            "stable",
        ]:
            try:
                job_response = await self.flat_manager.get_job(
                    pipeline.update_repo_job_id
                )
                job_status = JobStatus(job_response["status"])

                if job_status == JobStatus.ENDED:
                    logger.info(
                        "Reporting completed update-repo job status for published pipeline",
                        pipeline_id=str(pipeline.id),
                        update_repo_job_id=pipeline.update_repo_job_id,
                    )
                    await self._notify_flat_manager_job_completed(
                        pipeline,
                        "update-repo",
                        pipeline.update_repo_job_id,
                        success=True,
                    )
                    updated = True
                elif job_status == JobStatus.BROKEN:
                    logger.info(
                        "Reporting failed update-repo job status for published pipeline",
                        pipeline_id=str(pipeline.id),
                        update_repo_job_id=pipeline.update_repo_job_id,
                    )
                    await self._notify_flat_manager_job_completed(
                        pipeline,
                        "update-repo",
                        pipeline.update_repo_job_id,
                        success=False,
                    )
                    updated = True
            except Exception as e:
                logger.error(
                    "Failed to check update-repo job status for published pipeline",
                    pipeline_id=str(pipeline.id),
                    update_repo_job_id=pipeline.update_repo_job_id,
                    error=str(e),
                )

        return updated

    async def _notify_flat_manager_job_completed(
        self, pipeline: Pipeline, job_type: str, job_id: int, success: bool
    ) -> None:
        if pipeline.flat_manager_repo not in ["stable", "beta"]:
            return

        try:
            from app.services.github_notifier import GitHubNotifier

            github_notifier = GitHubNotifier()
            if success:
                state = "success"
                description = {
                    "commit": "Build committed",
                    "publish": "Build published",
                    "update-repo": "Repository updated",
                }.get(job_type, f"{job_type} completed")
            else:
                state = "failure"
                description = {
                    "commit": "Commit failed",
                    "publish": "Publish failed",
                    "update-repo": "Repository update failed",
                }.get(job_type, f"{job_type} failed")

            await github_notifier.notify_flat_manager_job_status(
                pipeline, job_type, job_id, state, description
            )
        except Exception as e:
            logger.error(
                f"Failed to notify {job_type} job completion",
                pipeline_id=str(pipeline.id),
                job_id=job_id,
                success=success,
                error=str(e),
            )
