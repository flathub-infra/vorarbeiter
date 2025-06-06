import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Pipeline, PipelineStatus
from app.utils.flat_manager import FlatManagerClient, JobStatus

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

        if pipeline.status == PipelineStatus.SUCCEEDED and pipeline.commit_job_id:
            if await self._process_succeeded_pipeline(db, pipeline):
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
                await self._notify_committed(pipeline)
                return True
            elif job_status == JobStatus.BROKEN:
                pipeline.status = PipelineStatus.FAILED
                logger.error(
                    "Commit job failed, marking pipeline as FAILED",
                    pipeline_id=str(pipeline.id),
                    commit_job_id=pipeline.commit_job_id,
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
