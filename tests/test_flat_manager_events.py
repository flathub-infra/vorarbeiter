import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models import FlatManagerWebhookDelivery, Pipeline, PipelineStatus
from app.services.flat_manager_events import PipelineNotReady, process_event
from app.utils.flat_manager import JobKind, JobStatus

delivery_ids = iter(range(1, 10_000))


def payload(
    kind: JobKind,
    job_id: int,
    *,
    status: JobStatus = JobStatus.ENDED,
    results: str | None = "{}",
    build_id: int | None = 50,
    created_at: datetime | None = None,
) -> dict:
    build = None
    if build_id is not None:
        build = {
            "id": build_id,
            "repo": "stable",
            "app_id": "org.test.App",
            "repo_state": 2,
            "repo_state_reason": None,
            "published_state": 0,
            "published_state_reason": None,
            "commit_job_id": None,
            "publish_job_id": None,
        }
    return {
        "id": next(delivery_ids),
        "type": "job.finished",
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "data": {
            "job": {
                "id": job_id,
                "kind": kind,
                "status": status,
                "repo": None,
                "results": results,
                "start_after": None,
            },
            "build": build,
        },
    }


@pytest.fixture
def env(db_session_maker, deferred):
    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db
            await db.commit()

    flat_manager = AsyncMock()
    notifier = AsyncMock()
    publication = AsyncMock()
    with (
        patch("app.services.flat_manager_events.get_db", get_test_db),
        patch(
            "app.services.job_monitor.get_flat_manager_client",
            return_value=flat_manager,
        ),
        patch("app.services.github_notifier.GitHubNotifier", return_value=notifier),
        patch("app.pipelines.build.BuildPipeline.handle_publication", publication),
    ):
        yield {
            "flat_manager": flat_manager,
            "notifier": notifier,
            "publication": publication,
            "deferred": deferred,
        }


async def add(db_session_maker, *rows) -> None:
    async with db_session_maker() as db:
        db.add_all(rows)
        await db.commit()


async def deliver(db_session_maker, body: dict) -> int:
    await add(
        db_session_maker,
        FlatManagerWebhookDelivery(
            id=body["id"],
            event_type=body["type"],
            payload=body,
            received_at=datetime.now(UTC),
        ),
    )
    await process_event(body["id"])
    return body["id"]


async def load(db_session_maker, model, key):
    async with db_session_maker() as db:
        return await db.get(model, key)


def make_pipeline(**fields) -> Pipeline:
    values = {
        "id": uuid.uuid4(),
        "app_id": "org.test.App",
        "status": PipelineStatus.SUCCEEDED,
        "flat_manager_repo": "stable",
        "params": {"repo": "flathub/org.test.App", "sha": "abc", "pr_number": None},
        "build_id": 50,
        "commit_job_id": 10,
        "created_at": datetime.now(UTC),
    }
    return Pipeline(**(values | fields))


def deferred_tasks(deferred) -> list[str]:
    return [call.args[1] for call in deferred.await_args_list]


@pytest.mark.asyncio
async def test_full_chain_commit_publish_update_repo(db_session_maker, env):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)

    await deliver(db_session_maker, payload(JobKind.COMMIT, 10))
    assert (await load(db_session_maker, Pipeline, pipeline.id)).status == (
        PipelineStatus.COMMITTED
    )
    assert deferred_tasks(env["deferred"]) == [
        "app.tasks.start_pending_builds",
        "app.tasks.publish_pipeline",
        "app.tasks.submit_pipeline_assessment",
        "app.tasks.notify_committed",
    ]
    publish = env["deferred"].await_args_list[1]
    assert publish.kwargs["lock"] == "publish:org.test.App:stable"

    await deliver(
        db_session_maker,
        payload(JobKind.PUBLISH, 20, results=json.dumps({"update-repo-job": 30})),
    )
    published = await load(db_session_maker, Pipeline, pipeline.id)
    assert published.status == PipelineStatus.PUBLISHING
    assert published.publish_job_id == 20
    assert published.update_repo_job_id == 30

    await deliver(db_session_maker, payload(JobKind.UPDATE_REPO, 30, build_id=None))
    final = await load(db_session_maker, Pipeline, pipeline.id)
    assert final.status == PipelineStatus.PUBLISHED
    env["publication"].assert_awaited_once()
    assert deferred_tasks(env["deferred"])[-1] == "app.tasks.publish_pipeline"
    env["flat_manager"].get_job.assert_not_called()


@pytest.mark.asyncio
async def test_check_event_retriggers_publish_for_committed_build(
    db_session_maker, env
):
    pipeline = make_pipeline(status=PipelineStatus.COMMITTED)
    await add(db_session_maker, pipeline)

    await deliver(db_session_maker, payload(JobKind.CHECK, 40))

    assert deferred_tasks(env["deferred"]) == ["app.tasks.publish_pipeline"]
    assert (await load(db_session_maker, Pipeline, pipeline.id)).status == (
        PipelineStatus.COMMITTED
    )


@pytest.mark.asyncio
async def test_update_repo_event_publishes_every_pipeline_sharing_the_job(
    db_session_maker, env
):
    pipelines = [
        make_pipeline(
            app_id=f"org.test.App{n}",
            build_id=50 + n,
            status=PipelineStatus.PUBLISHING,
            update_repo_job_id=30,
        )
        for n in range(2)
    ]
    await add(db_session_maker, *pipelines)

    await deliver(db_session_maker, payload(JobKind.UPDATE_REPO, 30, build_id=None))

    for pipeline in pipelines:
        loaded = await load(db_session_maker, Pipeline, pipeline.id)
        assert loaded.status == PipelineStatus.PUBLISHED
    assert env["publication"].await_count == 2


@pytest.mark.asyncio
async def test_duplicate_event_does_not_repeat_effects(db_session_maker, env):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    body = payload(JobKind.COMMIT, 10)

    delivery_id = await deliver(db_session_maker, body)
    await process_event(delivery_id)

    assert deferred_tasks(env["deferred"]).count("app.tasks.notify_committed") == 1
    assert (
        deferred_tasks(env["deferred"]).count("app.tasks.submit_pipeline_assessment")
        == 1
    )
    assert env["notifier"].notify_flat_manager_job_status.await_count == 1
    assert (
        await load(db_session_maker, FlatManagerWebhookDelivery, delivery_id)
    ).processed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [PipelineStatus.SUPERSEDED, PipelineStatus.CANCELLED, PipelineStatus.COMMITTED],
)
async def test_event_for_pipeline_in_another_state_is_ignored(
    db_session_maker, env, status
):
    pipeline = make_pipeline(status=status)
    await add(db_session_maker, pipeline)

    await deliver(db_session_maker, payload(JobKind.COMMIT, 10))

    assert (await load(db_session_maker, Pipeline, pipeline.id)).status == status
    assert env["deferred"].await_count == 0
    env["notifier"].notify_flat_manager_job_status.assert_not_called()


@pytest.mark.asyncio
async def test_event_before_job_id_is_stored_retries_then_resolves(
    db_session_maker, env
):
    body = payload(JobKind.COMMIT, 10, build_id=50)
    await add(
        db_session_maker,
        FlatManagerWebhookDelivery(
            id=body["id"],
            event_type=body["type"],
            payload=body,
            received_at=datetime.now(UTC),
        ),
    )

    with pytest.raises(PipelineNotReady):
        await process_event(body["id"])
    assert (
        await load(db_session_maker, FlatManagerWebhookDelivery, body["id"])
    ).processed_at is None

    pipeline = make_pipeline(commit_job_id=None)
    await add(db_session_maker, pipeline)
    await process_event(body["id"])

    loaded = await load(db_session_maker, Pipeline, pipeline.id)
    assert loaded.commit_job_id == 10
    assert loaded.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_unmatched_event_is_orphaned_after_window(db_session_maker, env):
    body = payload(
        JobKind.COMMIT, 10, created_at=datetime.now(UTC) - timedelta(minutes=16)
    )

    delivery_id = await deliver(db_session_maker, body)

    assert (
        await load(db_session_maker, FlatManagerWebhookDelivery, delivery_id)
    ).processed_at is not None


@pytest.mark.asyncio
async def test_redelivered_publish_event_reports_once(db_session_maker, env):
    pipeline = make_pipeline(status=PipelineStatus.COMMITTED, publish_job_id=20)
    await add(db_session_maker, pipeline)
    body = payload(JobKind.PUBLISH, 20, results=json.dumps({"update-repo-job": 30}))

    await deliver(db_session_maker, body)
    await deliver(db_session_maker, body | {"id": next(delivery_ids)})

    reports = [
        call.args[1:4]
        for call in env["notifier"].notify_flat_manager_job_status.await_args_list
    ]
    assert reports.count(("publish", 20, "success")) == 1


@pytest.mark.asyncio
async def test_failed_github_notification_keeps_commit(db_session_maker, env):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    env["notifier"].notify_flat_manager_job_status.side_effect = RuntimeError("down")

    await deliver(db_session_maker, payload(JobKind.COMMIT, 10))

    assert (await load(db_session_maker, Pipeline, pipeline.id)).status == (
        PipelineStatus.COMMITTED
    )
    assert "app.tasks.notify_committed" in deferred_tasks(env["deferred"])


@pytest.mark.asyncio
async def test_broken_commit_fetches_log_for_failure_issue(db_session_maker, env):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    env["flat_manager"].get_job.return_value = {"log": "boom", "status": 3}

    await deliver(
        db_session_maker, payload(JobKind.COMMIT, 10, status=JobStatus.BROKEN)
    )

    assert (await load(db_session_maker, Pipeline, pipeline.id)).status == (
        PipelineStatus.FAILED
    )
    env["flat_manager"].get_job.assert_awaited_once_with(10)
    issue = env["notifier"].create_stable_job_failure_issue.await_args
    assert issue.args[3]["log"] == "boom"


@pytest.mark.asyncio
async def test_unconsumed_job_kinds_are_acknowledged(db_session_maker, env):
    delivery_id = await deliver(
        db_session_maker, payload(JobKind.PRUNE, 99, build_id=None)
    )

    assert (
        await load(db_session_maker, FlatManagerWebhookDelivery, delivery_id)
    ).processed_at is not None


def test_retries_outlast_the_orphan_window_with_capped_waits():
    from types import SimpleNamespace

    from app.tasks import flat_manager_event

    strategy = flat_manager_event.retry_strategy
    assert strategy is not None
    now = datetime.now(UTC)
    waits = []
    for attempts in range(10):
        decision = strategy.get_retry_decision(
            exception=PipelineNotReady(1),
            job=SimpleNamespace(attempts=attempts, queue="flat-manager", task_name="t"),  # pyrefly: ignore[bad-argument-type]
        )
        if decision is None or decision.retry_at is None:
            break
        waits.append((decision.retry_at - now).total_seconds())

    assert max(waits) <= 301
    assert sum(waits) > 15 * 60
