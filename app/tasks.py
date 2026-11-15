from procrastinate import JobContext, builtin_tasks

from app.queue import app


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
