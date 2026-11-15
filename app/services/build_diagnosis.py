from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BuildDiagnosis, Pipeline
from app.queue import app


async def enqueue_diagnosis(db: AsyncSession, pipeline: Pipeline) -> None:
    try:
        async with db.begin_nested():
            db.add(BuildDiagnosis(pipeline_id=pipeline.id))
    except IntegrityError:
        return
    conn = (await (await db.connection()).get_raw_connection()).driver_connection
    await app.configure_task(
        "app.tasks.diagnose_build",
        queue="build-diagnosis",
        connection=conn,
        schedule_in={"seconds": 30},
    ).defer_async(pipeline_id=str(pipeline.id))
