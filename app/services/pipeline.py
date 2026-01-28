from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.schemas.pipelines import (
    PipelineResponse,
    PipelineSummary,
    PipelineType,
    ReprocheckStatus,
)
from app.services.job_monitor import JobMonitor
from app.utils.flat_manager import get_flat_manager_client

logger = structlog.get_logger(__name__)


class PipelineService:
    """Service for managing pipeline operations and business logic."""

    def __init__(self):
        self.flat_manager_client = get_flat_manager_client()
        self.job_monitor = JobMonitor()

    async def list_pipelines_with_filters(
        self,
        db: AsyncSession,
        app_id: str | None = None,
        pipeline_type: PipelineType = PipelineType.BUILD,
        status: PipelineStatus | None = None,
        reprocheck_status: ReprocheckStatus | None = None,
        triggered_by: PipelineTrigger | None = None,
        target_repo: str | None = None,
        limit: int = 10,
    ) -> list[Pipeline]:
        """
        List pipelines with filters and update job IDs where necessary.

        Args:
            db: Database session
            app_id: Filter by app ID prefix
            pipeline_type: Filter by pipeline type (build or reprocheck)
            status: Filter by pipeline status
            reprocheck_status: Filter by reprocheck result status
            triggered_by: Filter by trigger type
            target_repo: Filter by target repository
            limit: Maximum number of results (1-100)

        Returns:
            List of pipelines matching the filters
        """
        from sqlalchemy import String, cast, or_

        limit = min(max(1, limit), 100)

        stmt = select(Pipeline).order_by(Pipeline.created_at.desc())

        if app_id:
            stmt = stmt.where(Pipeline.app_id.startswith(app_id))

        if pipeline_type == PipelineType.BUILD:
            stmt = stmt.where(
                or_(
                    cast(Pipeline.params["workflow_id"], String) != "reprocheck.yml",
                    Pipeline.params["workflow_id"].is_(None),
                )
            )
        elif pipeline_type == PipelineType.REPROCHECK:
            stmt = stmt.where(
                cast(Pipeline.params["workflow_id"], String) == "reprocheck.yml"
            )

        if status:
            stmt = stmt.where(Pipeline.status == status)

        if reprocheck_status:
            status_code_map = {
                ReprocheckStatus.REPRODUCIBLE: "0",
                ReprocheckStatus.FAILURE: "1",
                ReprocheckStatus.UNREPRODUCIBLE: "42",
            }
            status_code = status_code_map[reprocheck_status]
            stmt = stmt.where(
                cast(Pipeline.params["reprocheck_result"]["status_code"], String)
                == status_code
            )

        if triggered_by:
            stmt = stmt.where(Pipeline.triggered_by == triggered_by)

        if target_repo:
            stmt = stmt.where(Pipeline.flat_manager_repo == target_repo)

        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        pipelines = list(result.scalars().all())

        return pipelines

    def pipeline_to_summary(self, pipeline: Pipeline) -> PipelineSummary:
        """Convert a Pipeline model to PipelineSummary schema."""
        params = pipeline.params or {}
        workflow_id = params.get("workflow_id", "build.yml")
        pipeline_type = (
            PipelineType.REPROCHECK
            if workflow_id == "reprocheck.yml"
            else PipelineType.BUILD
        )

        return PipelineSummary(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            type=pipeline_type,
            status=pipeline.status,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            triggered_by=pipeline.triggered_by,
            build_id=pipeline.build_id,
            commit_job_id=pipeline.commit_job_id,
            publish_job_id=pipeline.publish_job_id,
            update_repo_job_id=pipeline.update_repo_job_id,
            repro_pipeline_id=pipeline.repro_pipeline_id,
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
            repro_pipeline_id=pipeline.repro_pipeline_id,
            total_cost=pipeline.total_cost,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
        )

    def validate_status(self, status: Any) -> PipelineStatus:
        """Validate and convert status value."""
        try:
            return PipelineStatus(status)
        except ValueError:
            valid_values = [s.value for s in PipelineStatus]
            raise ValueError(
                f"Invalid status value: {status}. Valid values are: {valid_values}"
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
