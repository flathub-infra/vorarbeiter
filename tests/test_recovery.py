import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, patch

import pytest

from app.models import FlatManagerWebhookDelivery, Pipeline, PipelineStatus
from app.services import recovery
from app.utils.flat_manager import JobKind, JobStatus


@pytest.fixture
def env(db_session_maker, deferred):
    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db
            await db.commit()

    flat_manager = AsyncMock()
    notifier = AsyncMock()
    with (
        patch("app.services.recovery.get_db", get_test_db),
        patch(
            "app.services.job_monitor.get_flat_manager_client",
            return_value=flat_manager,
        ),
        patch("app.services.github_notifier.GitHubNotifier", return_value=notifier),
    ):
        yield {"flat_manager": flat_manager, "deferred": deferred}


async def add(db_session_maker, *rows) -> None:
    async with db_session_maker() as db:
        db.add_all(rows)
        await db.commit()


def delivery(delivery_id: int, age: timedelta, processed: bool = False):
    received_at = datetime.now(UTC) - age
    return FlatManagerWebhookDelivery(
        id=delivery_id,
        event_type="job.finished",
        payload={"id": delivery_id},
        received_at=received_at,
        processed_at=received_at if processed else None,
    )


def make_pipeline(**fields) -> Pipeline:
    values = {
        "id": uuid.uuid4(),
        "app_id": "org.test.App",
        "status": PipelineStatus.COMMITTED,
        "flat_manager_repo": "stable",
        "params": {"repo": "flathub/org.test.App", "sha": "abc", "pr_number": None},
        "created_at": datetime.now(UTC) - timedelta(minutes=11),
    }
    return Pipeline(**(values | fields))


def deferred_tasks(deferred) -> list[str]:
    return [call.args[1] for call in deferred.await_args_list]


@pytest.mark.asyncio
async def test_unprocessed_inbox_rows_older_than_gap_are_reenqueued(
    db_session_maker, env
):
    await add(
        db_session_maker,
        delivery(1, timedelta(minutes=11)),
        delivery(2, timedelta(minutes=1)),
        delivery(3, timedelta(hours=1), processed=True),
    )

    counts = await recovery.recover_stale()

    env["deferred"].assert_awaited_once_with(
        ANY,
        "app.tasks.flat_manager_event",
        lock="flat-manager-event:1",
        delivery_id=1,
    )
    assert counts["undelivered_events"] == 1
    assert counts["abandoned_events"] == 0


@pytest.mark.asyncio
async def test_inbox_rows_older_than_a_day_are_counted_and_kept(db_session_maker, env):
    await add(
        db_session_maker,
        delivery(1, timedelta(hours=25)),
        delivery(2, timedelta(hours=23)),
        delivery(3, timedelta(hours=48), processed=True),
    )

    counts = await recovery.recover_stale()

    assert counts["abandoned_events"] == 1
    assert counts["undelivered_events"] == 2
    async with db_session_maker() as db:
        for delivery_id in (1, 2, 3):
            assert await db.get(FlatManagerWebhookDelivery, delivery_id) is not None


@pytest.mark.asyncio
async def test_committed_stable_pipeline_without_publish_job_defers_publish(
    db_session_maker, env
):
    stable = make_pipeline(app_id="org.test.Stable")
    test_repo = make_pipeline(app_id="org.test.Test", flat_manager_repo="test")
    fresh = make_pipeline(
        app_id="org.test.Fresh", created_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    await add(db_session_maker, stable, test_repo, fresh)

    counts = await recovery.recover_stale()

    env["deferred"].assert_awaited_once_with(
        ANY,
        "app.tasks.publish_pipeline",
        lock="publish:org.test.Stable:stable",
        app_id="org.test.Stable",
        flat_manager_repo="stable",
    )
    assert counts["unpublished"] == 1


@pytest.mark.asyncio
async def test_recover_pipeline_commits_pipeline_whose_commit_job_ended(
    db_session_maker, env
):
    pipeline = make_pipeline(
        status=PipelineStatus.SUCCEEDED, build_id=50, commit_job_id=10
    )
    await add(db_session_maker, pipeline)
    env["flat_manager"].get_job.return_value = {
        "id": 10,
        "kind": JobKind.COMMIT,
        "status": JobStatus.ENDED,
    }

    counts = await recovery.recover_stale()

    assert counts["stale_pipelines"] == 1
    assert deferred_tasks(env["deferred"]) == ["app.tasks.recover_pipeline"]
    env["deferred"].reset_mock()

    assert await recovery.recover_pipeline(pipeline.id) is True

    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline.id)).status == PipelineStatus.COMMITTED
    assert deferred_tasks(env["deferred"]) == [
        "app.tasks.start_pending_builds",
        "app.tasks.publish_pipeline",
        "app.tasks.submit_pipeline_assessment",
        "app.tasks.notify_committed",
    ]


@pytest.mark.asyncio
async def test_sweep_without_gaps_defers_nothing(db_session_maker, env):
    now = datetime.now(UTC)
    await add(
        db_session_maker,
        make_pipeline(
            status=PipelineStatus.SUCCEEDED,
            build_id=50,
            commit_job_id=10,
            created_at=now - timedelta(minutes=1),
        ),
        make_pipeline(
            status=PipelineStatus.COMMITTED,
            created_at=now - timedelta(minutes=1),
        ),
        make_pipeline(
            status=PipelineStatus.RUNNING,
            started_at=now - timedelta(minutes=30),
            created_at=now - timedelta(minutes=30),
        ),
        make_pipeline(
            status=PipelineStatus.PUBLISHED,
            created_at=now - timedelta(hours=2),
        ),
        delivery(1, timedelta(minutes=1)),
    )

    counts = await recovery.recover_stale()

    env["deferred"].assert_not_awaited()
    assert not any(
        counts[key]
        for key in (
            "expired",
            "debounced",
            "stale_pipelines",
            "unpublished",
            "undelivered_events",
            "abandoned_events",
        )
    )
