import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.schemas.pipelines import PipelineResponse, PipelineSummary
from app.services.job_monitor import JobMonitor
from app.utils.flat_manager import FlatManagerClient

logger = structlog.get_logger(__name__)


class PipelineService:
    """Service for managing pipeline operations and business logic."""

    def __init__(self):
        self.flat_manager_client = FlatManagerClient(
            url=settings.flat_manager_url,
            token=settings.flat_manager_token,
        )
        self.job_monitor = JobMonitor()

    async def update_pipeline_job_ids(self, pipeline: Pipeline) -> bool:
        """
        Update pipeline job IDs from flat-manager if they're missing.

        Args:
            pipeline: The pipeline to update

        Returns:
            True if any updates were made, False otherwise
        """
        if pipeline.commit_job_id is not None and pipeline.publish_job_id is not None:
            return False

        if not pipeline.build_id:
            return False

        try:
            build_info = await self.flat_manager_client.get_build_info(
                pipeline.build_id
            )
            build_data = build_info.get("build", {})

            commit_job_id = build_data.get("commit_job_id")
            publish_job_id = build_data.get("publish_job_id")

            updated = False
            if commit_job_id is not None and pipeline.commit_job_id is None:
                pipeline.commit_job_id = commit_job_id
                updated = True

            if publish_job_id is not None and pipeline.publish_job_id is None:
                pipeline.publish_job_id = publish_job_id
                updated = True

            return updated
        except Exception as e:
            logger.error(
                "Failed to fetch job IDs from flat-manager",
                pipeline_id=str(pipeline.id),
                build_id=pipeline.build_id,
                error=str(e),
            )
            return False

    async def get_pipeline_with_job_updates(
        self, db: AsyncSession, pipeline_id: uuid.UUID
    ) -> Pipeline | None:
        """
        Get a pipeline and update its job IDs if necessary.

        Args:
            db: Database session
            pipeline_id: Pipeline ID to fetch

        Returns:
            Pipeline instance or None if not found
        """
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            return None

        if (
            pipeline.commit_job_id is None or pipeline.publish_job_id is None
        ) and pipeline.build_id:
            if await self.update_pipeline_job_ids(pipeline):
                await db.commit()

        if await self.job_monitor.check_and_update_pipeline_jobs(db, pipeline):
            await db.commit()

        return pipeline

    async def list_pipelines_with_filters(
        self,
        db: AsyncSession,
        app_id: str | None = None,
        status_filter: PipelineStatus | None = None,
        triggered_by: PipelineTrigger | None = None,
        target_repo: str | None = None,
        limit: int = 10,
    ) -> list[Pipeline]:
        """
        List pipelines with filters and update job IDs where necessary.

        Args:
            db: Database session
            app_id: Filter by app ID prefix
            status_filter: Filter by pipeline status
            triggered_by: Filter by trigger type
            target_repo: Filter by target repository
            limit: Maximum number of results (1-100)

        Returns:
            List of pipelines matching the filters
        """
        limit = min(max(1, limit), 100)

        stmt = select(Pipeline).order_by(Pipeline.created_at.desc())

        if app_id:
            stmt = stmt.where(Pipeline.app_id.startswith(app_id))

        if status_filter:
            stmt = stmt.where(Pipeline.status == status_filter)

        if triggered_by:
            stmt = stmt.where(Pipeline.triggered_by == triggered_by)

        if target_repo:
            stmt = stmt.where(Pipeline.flat_manager_repo == target_repo)

        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        pipelines = list(result.scalars().all())

        updated_job_ids = False
        updated_statuses = False
        for pipeline in pipelines:
            if (
                pipeline.commit_job_id is None or pipeline.publish_job_id is None
            ) and pipeline.build_id:
                if await self.update_pipeline_job_ids(pipeline):
                    updated_job_ids = True

            if await self.job_monitor.check_and_update_pipeline_jobs(db, pipeline):
                updated_statuses = True

        if updated_job_ids or updated_statuses:
            await db.commit()

        return pipelines

    def pipeline_to_summary(self, pipeline: Pipeline) -> PipelineSummary:
        """Convert a Pipeline model to PipelineSummary schema."""
        return PipelineSummary(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            triggered_by=pipeline.triggered_by,
            build_id=pipeline.build_id,
            commit_job_id=pipeline.commit_job_id,
            publish_job_id=pipeline.publish_job_id,
            update_repo_job_id=pipeline.update_repo_job_id,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
        )

    def pipeline_to_response(self, pipeline: Pipeline) -> PipelineResponse:
        """Convert a Pipeline model to PipelineResponse schema."""
        return PipelineResponse(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by,
            log_url=pipeline.log_url,
            build_id=pipeline.build_id,
            commit_job_id=pipeline.commit_job_id,
            publish_job_id=pipeline.publish_job_id,
            update_repo_job_id=pipeline.update_repo_job_id,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
        )

    def validate_status_filter(self, status_filter: Any) -> PipelineStatus:
        """Validate and convert status filter value."""
        try:
            return PipelineStatus(status_filter)
        except ValueError:
            valid_values = [s.value for s in PipelineStatus]
            raise ValueError(
                f"Invalid status value: {status_filter}. Valid values are: {valid_values}"
            )

    def validate_trigger_filter(self, triggered_by: Any) -> PipelineTrigger:
        """Validate and convert trigger filter value."""
        try:
            return PipelineTrigger(triggered_by)
        except ValueError:
            valid_values = [t.value for t in PipelineTrigger]
            raise ValueError(
                f"Invalid triggered_by value: {triggered_by}. Valid values are: {valid_values}"
            )

    async def trigger_manual_pipeline(
        self, app_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Trigger a new pipeline manually.

        Args:
            app_id: Application ID
            params: Pipeline parameters

        Returns:
            Dictionary with pipeline creation details
        """
        from app.pipelines import BuildPipeline

        build_pipeline = BuildPipeline()

        pipeline = await build_pipeline.create_pipeline(
            app_id=app_id,
            params=params,
            webhook_event_id=None,
        )

        from app.database import get_db

        async with get_db() as db:
            db_pipeline = await db.get(Pipeline, pipeline.id)
            if db_pipeline is None:
                raise ValueError(f"Pipeline {pipeline.id} not found")
            db_pipeline.triggered_by = PipelineTrigger.MANUAL
            await db.flush()
            pipeline = db_pipeline

        pipeline = await build_pipeline.start_pipeline(
            pipeline_id=pipeline.id,
        )

        return {
            "status": "created",
            "pipeline_id": str(pipeline.id),
            "app_id": pipeline.app_id,
            "pipeline_status": pipeline.status.value,
        }
