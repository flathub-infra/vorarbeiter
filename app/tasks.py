import uuid

from procrastinate import JobContext, RetryStrategy, builtin_tasks

from app.pipelines import BuildPipeline
from app.queue import app
from app.services import (
    build_diagnosis,
    flat_manager_events,
    github_notifier,
    permission_assessment,
    publishing,
    publishing_service,
    run_reconciler,
)
from app.services.task_queue import QueueRetryStrategy
from app.utils.github import GitHubUnavailable, retry_after_hint, transient_failure


@app.task(
    name=build_diagnosis.TASK_NAME,
    queue=build_diagnosis.QUEUE,
    pass_context=True,
    retry=RetryStrategy(
        wait=60,
        retry_exceptions={
            build_diagnosis.RunNotCompleted,
            build_diagnosis.TransientError,
        },
    ),
)
async def diagnose_build(
    context: JobContext, pipeline_id: str, rate_limit_retries: int = 0
) -> None:
    await build_diagnosis.diagnose(
        uuid.UUID(pipeline_id), context.job.attempts, rate_limit_retries
    )


@app.task(queue="builds")
async def build_finished(
    pipeline_id: str, status_value: str, infrastructure_cancellation: bool
) -> None:
    await BuildPipeline().finish_build(
        uuid.UUID(pipeline_id), status_value, infrastructure_cancellation
    )


@app.task(queue="builds")
async def retry_reprocheck(pipeline_id: str, callback_data: dict) -> None:
    await BuildPipeline().retry_reprocheck(uuid.UUID(pipeline_id), callback_data)


@app.task(queue="builds")
async def start_pending_builds() -> None:
    await BuildPipeline().start_pending_builds()


@app.task(queue="builds")
async def start_debounced_build(pipeline_id: str) -> None:
    await BuildPipeline().start_debounced_build(uuid.UUID(pipeline_id))


@app.task(
    queue="permission",
    retry=QueueRetryStrategy(
        wait=300,
        retry_exceptions={permission_assessment.PermissionAssessmentPending},
    ),
)
async def submit_pipeline_assessment(pipeline_id: str) -> None:
    status = await permission_assessment.submit_pipeline_assessment(
        uuid.UUID(pipeline_id)
    )
    if status == "failed":
        raise permission_assessment.PermissionAssessmentPending(pipeline_id)


@app.task(queue="permission", retry=QueueRetryStrategy(max_attempts=5, wait=60))
async def refresh_permission_review(
    source_repository: str, pull_request_number: int
) -> None:
    await permission_assessment.refresh_permission_review(
        source_repository, pull_request_number
    )


@app.task(
    queue="github",
    retry=QueueRetryStrategy(
        max_attempts=8, exponential_wait=2, retry_exceptions={GitHubUnavailable}
    ),
)
async def notify_committed(pipeline_id: str) -> None:
    retry_after_hint.set(None)
    transient_failure.set(False)
    await github_notifier.notify_committed(uuid.UUID(pipeline_id))


@app.task(
    queue="maintenance",
    retry=QueueRetryStrategy(
        max_attempts=5, wait=60, retry_exceptions={run_reconciler.RunJobsUnavailable}
    ),
)
async def reconcile_run(pipeline_id: str, conclusion: str | None) -> None:
    await run_reconciler.reconcile_run(uuid.UUID(pipeline_id), conclusion)


@app.task(
    name=flat_manager_events.TASK_NAME,
    queue=flat_manager_events.QUEUE,
    retry=QueueRetryStrategy(max_attempts=8, exponential_wait=3, max_wait=300),
)
async def flat_manager_event(delivery_id: int) -> None:
    await flat_manager_events.process_event(delivery_id)


@app.task(
    name=publishing.PUBLISH_TASK,
    queue="flat-manager",
    retry=QueueRetryStrategy(
        max_attempts=10,
        exponential_wait=2,
        max_wait=900,
        retry_exceptions={publishing.PublishNotReady},
    ),
)
async def publish_pipeline(app_id: str, flat_manager_repo: str) -> None:
    await publishing_service.publish_pipeline(app_id, flat_manager_repo)


@app.periodic(cron="*/10 * * * *")  # pyrefly: ignore[bad-argument-type]
@app.task(queue="maintenance", queueing_lock="retry_stalled_jobs", pass_context=True)
async def retry_stalled_jobs(context: JobContext, timestamp: int) -> None:
    for job in await app.job_manager.get_stalled_jobs():
        await app.job_manager.retry_job(job)


@app.periodic(cron="0 * * * *")  # pyrefly: ignore[bad-argument-type]
@app.task(queue="maintenance", queueing_lock="remove_old_jobs", pass_context=True)
async def remove_old_jobs(context: JobContext, timestamp: int) -> None:
    await builtin_tasks.remove_old_jobs(
        context,
        max_hours=168,
        remove_failed=True,
        remove_cancelled=True,
        remove_aborted=True,
    )
