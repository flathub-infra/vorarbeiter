import uuid

from procrastinate import JobContext, RetryStrategy, builtin_tasks

from app.queue import app
from app.services import build_diagnosis


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
