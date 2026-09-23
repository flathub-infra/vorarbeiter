from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.schemas.pipelines import (
    PipelineResponse,
    PipelineSummary,
    PipelineType,
    ReprocheckStatus,
)
from app.services.job_monitor import JobMonitor
from app.services.reprocheck_notification import (
    REPROCHECK_BUILD_FAILED,
    REPROCHECK_REPRODUCIBLE,
    REPROCHECK_UNREPRODUCIBLE,
)
from app.utils.flat_manager import get_flat_manager_client


class PipelineService:
    def __init__(self):
        self.flat_manager_client = get_flat_manager_client()
        self.job_monitor = JobMonitor()

    async def list_pipelines_with_filters(
        self,
        db: AsyncSession,
        app_id: str | None = None,
        app_id_match: Literal["prefix", "contains", "exact"] = "prefix",
        pipeline_type: PipelineType = PipelineType.BUILD,
        status: PipelineStatus | None = None,
        reprocheck_status: ReprocheckStatus | None = None,
        triggered_by: PipelineTrigger | None = None,
        target_repo: str | None = None,
        limit: int = 10,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        group: Literal["in-progress", "awaiting-publishing", "completed"] | None = None,
        offset: int = 0,
    ) -> list[Pipeline]:
        workflow_id = Pipeline.params["workflow_id"].as_string()

        limit = min(max(1, limit), 100)

        stmt = select(Pipeline).order_by(Pipeline.created_at.desc(), Pipeline.id.desc())

        if app_id:
            if app_id_match == "exact":
                stmt = stmt.where(Pipeline.app_id == app_id)
            elif app_id_match == "contains":
                escaped = (
                    app_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                stmt = stmt.where(Pipeline.app_id.ilike(f"%{escaped}%", escape="\\"))
            else:
                stmt = stmt.where(Pipeline.app_id.startswith(app_id))

        if pipeline_type == PipelineType.BUILD:
            stmt = stmt.where(
                or_(
                    workflow_id != "reprocheck.yml",
                    workflow_id.is_(None),
                )
            )
        elif pipeline_type == PipelineType.REPROCHECK:
            stmt = stmt.where(workflow_id == "reprocheck.yml")

        if status:
            stmt = stmt.where(Pipeline.status == status)

        if reprocheck_status:
            status_code_map = {
                ReprocheckStatus.REPRODUCIBLE: REPROCHECK_REPRODUCIBLE,
                ReprocheckStatus.FAILURE: REPROCHECK_BUILD_FAILED,
                ReprocheckStatus.UNREPRODUCIBLE: REPROCHECK_UNREPRODUCIBLE,
            }
            status_code = status_code_map[reprocheck_status]
            stmt = stmt.where(
                Pipeline.params["reprocheck_result"]["status_code"].as_string()
                == status_code
            )

        if triggered_by:
            stmt = stmt.where(Pipeline.triggered_by == triggered_by)

        if target_repo:
            stmt = stmt.where(Pipeline.flat_manager_repo == target_repo)
        if date_from is not None:
            stmt = stmt.where(Pipeline.started_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(Pipeline.started_at <= date_to)

        if group:
            awaiting = and_(
                Pipeline.status == PipelineStatus.COMMITTED,
                Pipeline.flat_manager_repo.in_(("stable", "beta")),
            )
            in_progress = Pipeline.status.in_(
                (
                    PipelineStatus.PENDING,
                    PipelineStatus.RUNNING,
                    PipelineStatus.SUCCEEDED,
                    PipelineStatus.PUBLISHING,
                )
            )
            stmt = stmt.where(Pipeline.app_id != "flathub")
            if group == "in-progress":
                stmt = stmt.where(in_progress)
            elif group == "awaiting-publishing":
                stmt = stmt.where(awaiting)
            else:
                stmt = stmt.where(
                    ~in_progress,
                    or_(~awaiting, Pipeline.flat_manager_repo.is_(None)),
                )

        stmt = stmt.offset(offset).limit(limit)
        result = await db.execute(stmt)
        pipelines = list(result.scalars().all())

        return pipelines

    def pipeline_to_summary(
        self, pipeline: Pipeline, repro_pipeline: Pipeline | None = None
    ) -> PipelineSummary:
        params = pipeline.params or {}
        workflow_id = params.get("workflow_id", "build.yml")
        pipeline_type = (
            PipelineType.REPROCHECK
            if workflow_id == "reprocheck.yml"
            else PipelineType.BUILD
        )
        pr_number = params.get("pr_number")
        repro_result = (
            repro_pipeline.params.get("reprocheck_result")
            if repro_pipeline and isinstance(repro_pipeline.params, dict)
            else None
        )
        if not isinstance(repro_result, dict):
            repro_result = {}

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
            source_repo=params.get("repo")
            if isinstance(params.get("repo"), str)
            else None,
            sha=params.get("sha") if isinstance(params.get("sha"), str) else None,
            pr_number=str(pr_number)
            if isinstance(pr_number, (str, int)) and not isinstance(pr_number, bool)
            else None,
            log_url=pipeline.log_url,
            failure_issue_url=pipeline.failure_issue_url,
            reprocheck_status_code=repro_result.get("status_code")
            if isinstance(repro_result.get("status_code"), str)
            else None,
            reprocheck_result_url=repro_result.get("result_url")
            if isinstance(repro_result.get("result_url"), str)
            else None,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
        )

    def pipeline_to_response(self, pipeline: Pipeline) -> PipelineResponse:
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
            failure_issue_url=pipeline.failure_issue_url,
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
        try:
            return PipelineStatus(status)
        except ValueError:
            valid_values = [s.value for s in PipelineStatus]
            raise ValueError(
                f"Invalid status value: {status}. Valid values are: {valid_values}"
            )

    def validate_trigger_filter(self, triggered_by: Any) -> PipelineTrigger:
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

        pipeline = await build_pipeline.prepare_pipeline_for_start(pipeline.id)
        await build_pipeline.supersede_conflicting_test_pipelines(pipeline.id)
        should_queue = await build_pipeline.should_queue_test_build(pipeline.id)

        if not should_queue:
            pipeline = await build_pipeline.start_pipeline(
                pipeline_id=pipeline.id,
            )

        return {
            "status": "created",
            "pipeline_id": str(pipeline.id),
            "app_id": pipeline.app_id,
            "pipeline_status": pipeline.status.value,
        }
