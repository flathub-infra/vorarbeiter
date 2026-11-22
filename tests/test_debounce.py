import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from procrastinate.testing import InMemoryConnector

from app.models import Pipeline, PipelineStatus
from app.pipelines.build import BuildPipeline, defer_start_pending
from app.queue import app as queue_app


@pytest.fixture
def connector(db_session_maker):
    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db
            await db.commit()

    connector = InMemoryConnector()
    with (
        patch("app.pipelines.build.get_db", get_test_db),
        queue_app.replace_connector(connector),
    ):
        yield connector


def jobs(connector, task_name):
    return [j for j in connector.jobs.values() if j["task_name"] == task_name]


async def add_pending(db_session_maker, start_after, status=PipelineStatus.PENDING):
    pipeline_id = uuid.uuid4()
    async with db_session_maker() as db:
        db.add(
            Pipeline(
                id=pipeline_id,
                app_id="org.test.App",
                status=status,
                flat_manager_repo="test",
                params={"ref": "refs/pull/1/head", "start_after": start_after},
                provider_data={},
            )
        )
        await db.commit()
    return pipeline_id


@pytest.mark.asyncio
async def test_create_pipeline_schedules_debounce_at_start_after(connector):
    start_after = datetime.now(UTC).timestamp() + 300

    pipeline = await BuildPipeline().create_pipeline(
        "org.test.App", {"start_after": start_after}
    )

    (job,) = jobs(connector, "app.tasks.start_debounced_build")
    assert job["queue_name"] == "builds"
    assert job["lock"] == job["queueing_lock"] == f"debounce:{pipeline.id}"
    assert job["args"] == {"pipeline_id": str(pipeline.id)}
    assert job["scheduled_at"].timestamp() == pytest.approx(start_after)


@pytest.mark.asyncio
async def test_create_pipeline_without_debounce_defers_nothing(connector):
    await BuildPipeline().create_pipeline("org.test.App", {})

    assert not connector.jobs


@pytest.mark.asyncio
async def test_debounce_task_reschedules_when_refresh_moved_start_after(
    db_session_maker, connector
):
    refreshed = datetime.now(UTC).timestamp() + 300
    pipeline_id = await add_pending(db_session_maker, refreshed)
    build = BuildPipeline()
    build.start_pipeline = AsyncMock()

    await build.start_debounced_build(pipeline_id)

    build.start_pipeline.assert_not_awaited()
    (job,) = jobs(connector, "app.tasks.start_debounced_build")
    assert job["scheduled_at"].timestamp() == pytest.approx(refreshed)
    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline_id)).status == PipelineStatus.PENDING


@pytest.mark.asyncio
async def test_debounce_task_starts_due_pipeline(db_session_maker, connector):
    pipeline_id = await add_pending(db_session_maker, datetime.now(UTC).timestamp() - 1)
    build = BuildPipeline()
    build.start_pipeline = AsyncMock()

    await build.start_debounced_build(pipeline_id)

    build.start_pipeline.assert_awaited_once_with(pipeline_id)
    assert not connector.jobs


@pytest.mark.asyncio
async def test_debounce_task_ignores_pipeline_that_left_pending(
    db_session_maker, connector
):
    pipeline_id = await add_pending(
        db_session_maker,
        datetime.now(UTC).timestamp() - 1,
        status=PipelineStatus.CANCELLED,
    )
    build = BuildPipeline()
    build.start_pipeline = AsyncMock()

    await build.start_debounced_build(pipeline_id)

    build.start_pipeline.assert_not_awaited()
    assert not connector.jobs


@pytest.mark.asyncio
async def test_state_changes_in_a_row_queue_one_pending_start(connector):
    await defer_start_pending()
    await defer_start_pending()

    assert len(jobs(connector, "app.tasks.start_pending_builds")) == 1
