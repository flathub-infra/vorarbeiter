import uuid
from typing import Any

import structlog
from sqlalchemy import or_, select

from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.pipelines import BuildPipeline
from app.services.github_actions import GitHubActionsService
from app.services.job_monitor import JobMonitor
from app.services.task_queue import defer

logger = structlog.get_logger(__name__)

RECONCILE_RUN_TASK = "app.tasks.reconcile_run"
DISPATCH_REPOSITORY = "flathub-infra/vorarbeiter"
CALLBACK_STATUSES = {"success": "success", "cancelled": "cancelled"}
CALLBACK_JOB = "callback"
FAILED_CONCLUSIONS = {"failure", "timed_out"}


class RunJobsUnavailable(Exception):
    pass


def status_from_jobs(jobs: list[dict[str, Any]]) -> str:
    jobs = [job for job in jobs if job.get("name") != CALLBACK_JOB]
    conclusions = [job.get("conclusion") for job in jobs]
    if FAILED_CONCLUSIONS & set(conclusions):
        return "failure"
    if "cancelled" in conclusions:
        return "cancelled"
    builds = [job for job in jobs if str(job.get("name")).startswith("build-")]
    if builds and all(job.get("conclusion") == "success" for job in builds):
        return "success"
    return "failure"


async def enqueue_run_reconciliation(payload: dict[str, Any]) -> uuid.UUID | None:
    run = payload.get("workflow_run") or {}
    run_id = run.get("id")
    if (
        payload.get("action") != "completed"
        or type(run_id) is not int
        or payload["repository"]["full_name"] != DISPATCH_REPOSITORY
    ):
        return None

    async with get_db() as db:
        pipeline_id = await db.scalar(
            select(Pipeline.id).where(
                Pipeline.status == PipelineStatus.RUNNING,
                or_(
                    Pipeline.provider_data["run_id"].as_string() == str(run_id),
                    Pipeline.log_url.like(f"%/actions/runs/{run_id}"),
                ),
            )
        )
        if pipeline_id is not None:
            await defer(
                db,
                RECONCILE_RUN_TASK,
                lock=f"reconcile-run:{pipeline_id}",
                pipeline_id=str(pipeline_id),
                conclusion=run.get("conclusion"),
            )
    return pipeline_id


async def reconcile_run(pipeline_id: uuid.UUID, conclusion: str | None) -> None:
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
    if pipeline is None or pipeline.status != PipelineStatus.RUNNING:
        return

    status = CALLBACK_STATUSES.get(conclusion or "")
    if status is None and pipeline.params.get("workflow_id") == "reprocheck.yml":
        logger.warning(
            "Leaving reprocheck pipeline running: its result is only in the lost callback",
            pipeline_id=str(pipeline_id),
            conclusion=conclusion,
        )
        return
    if status is None:
        status = await job_status(pipeline)
    logger.warning(
        "Completing running pipeline from workflow run event",
        pipeline_id=str(pipeline_id),
        conclusion=conclusion,
    )
    build_pipeline = BuildPipeline()
    if pipeline.params.get("workflow_id") == "reprocheck.yml":
        await build_pipeline.handle_reprocheck_callback(
            pipeline_id,
            {
                "status": status,
                "build_pipeline_id": pipeline.params.get("build_pipeline_id"),
            },
        )
    else:
        await build_pipeline.handle_status_callback(pipeline_id, {"status": status})


async def job_status(pipeline: Pipeline) -> str:
    run_info = JobMonitor._get_github_run_info(pipeline)
    jobs = (
        await GitHubActionsService().get_workflow_run_jobs(*run_info)
        if run_info
        else None
    )
    if jobs is None:
        raise RunJobsUnavailable(str(pipeline.id))
    return status_from_jobs(jobs)
