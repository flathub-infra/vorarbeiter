import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select

from app.config import settings
from app.models import Pipeline, PipelineStatus
from app.pipelines.build import BuildPipeline
from app.queue import app as queue_app
from app.services import run_reconciler


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
        patch("app.services.run_reconciler.get_db", get_test_db),
        patch.object(settings, "ff_build_diagnosis", False),
        queue_app.replace_connector(connector),
    ):
        yield connector


@pytest.fixture
def flat_manager():
    client = AsyncMock()
    client.get_build_info.return_value = {"build": {"commit_job_id": 5}}
    with patch("app.pipelines.build.get_flat_manager_client", return_value=client):
        yield client


@pytest.fixture
def notifier():
    instance = AsyncMock()
    with patch("app.pipelines.build.GitHubNotifier", return_value=instance):
        yield instance


async def add_pipeline(db_session_maker, status=PipelineStatus.RUNNING) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_session_maker() as db:
        db.add(
            Pipeline(
                id=pipeline_id,
                app_id="org.test.App",
                status=status,
                params={"repo": "flathub/org.test.App"},
                provider_data={},
                callback_token="token",
                build_id=1,
                flat_manager_repo="test",
            )
        )
        await db.commit()
    return pipeline_id


def jobs(connector, task_name):
    return [j for j in connector.jobs.values() if j["task_name"] == task_name]


async def status_callback(pipeline_id, status="success"):
    with patch(
        "app.services.github_actions.GitHubActionsService.check_run_was_cancelled",
        AsyncMock(return_value=False),
    ):
        return await BuildPipeline().handle_status_callback(
            pipeline_id, {"status": status}
        )


@pytest.mark.asyncio
async def test_status_commits_before_downstream_effects(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker)

    _, updates = await status_callback(pipeline_id)

    assert updates == {"pipeline_status": "success"}
    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline_id)).status == PipelineStatus.SUCCEEDED
    assert len(jobs(connector, "app.tasks.build_finished")) == 1
    flat_manager.commit.assert_not_awaited()
    notifier.handle_build_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_status_callback_does_not_defer_again(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker)
    await status_callback(pipeline_id)

    with pytest.raises(ValueError, match="Pipeline status already finalized"):
        await status_callback(pipeline_id)

    assert len(jobs(connector, "app.tasks.build_finished")) == 1


@pytest.mark.asyncio
async def test_replayed_build_finished_neither_renotifies_nor_fails(
    db_session_maker, connector, flat_manager, notifier
):
    committing = httpx.Response(
        400,
        json={"error-type": "wrong-repo-state", "current-state": "committing"},
        request=httpx.Request("POST", "https://hub.example/commit"),
    )
    flat_manager.commit.side_effect = [
        None,
        httpx.HTTPStatusError(
            "bad request", request=committing.request, response=committing
        ),
    ]
    pipeline_id = await add_pipeline(db_session_maker)
    await status_callback(pipeline_id)
    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])

    await build.finish_build(pipeline_id, "success", False)
    await build.finish_build(pipeline_id, "success", False)

    assert flat_manager.commit.await_count == 2
    notifier.handle_build_completion.assert_awaited_once()
    async with db_session_maker() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
    assert pipeline.commit_job_id == 5
    assert pipeline.provider_data["build_finished_notified"] is True


@pytest.mark.asyncio
async def test_failed_commit_state_is_not_treated_as_committed(
    db_session_maker, connector, flat_manager, notifier
):
    failed = httpx.Response(
        400,
        json={"error-type": "wrong-repo-state", "current-state": "failed"},
        request=httpx.Request("POST", "https://hub.example/commit"),
    )
    flat_manager.commit.side_effect = httpx.HTTPStatusError(
        "bad request", request=failed.request, response=failed
    )
    pipeline_id = await add_pipeline(db_session_maker, status=PipelineStatus.SUCCEEDED)
    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])

    await build.finish_build(pipeline_id, "success", False)

    flat_manager.get_build_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_run_still_reports_to_pull_request(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker)
    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])

    await build.finish_build(pipeline_id, "cancelled", False)

    notifier.handle_build_completion.assert_awaited_once()
    assert notifier.handle_build_completion.await_args.args[1] == "cancelled"


def workflow_run_payload(run_id, conclusion="success", repo=None):
    return {
        "action": "completed",
        "workflow_run": {"id": run_id, "conclusion": conclusion},
        "repository": {"full_name": repo or run_reconciler.DISPATCH_REPOSITORY},
    }


@pytest.mark.asyncio
async def test_workflow_run_for_running_pipeline_synthesizes_status(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker)
    async with db_session_maker() as db:
        (await db.get(Pipeline, pipeline_id)).provider_data = {"run_id": "777"}
        await db.commit()

    assert (
        await run_reconciler.enqueue_run_reconciliation(workflow_run_payload(777))
        == pipeline_id
    )
    (job,) = jobs(connector, "app.tasks.reconcile_run")
    assert job["queue_name"] == "maintenance"
    assert job["lock"] == f"reconcile-run:{pipeline_id}"


def run_jobs(**conclusions):
    return [{"name": name, "conclusion": value} for name, value in conclusions.items()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("jobs_payload", "status", "pipeline_status"),
    [
        (
            run_jobs(**{"build-x86_64": "success", "callback": "failure"}),
            "success",
            PipelineStatus.SUCCEEDED,
        ),
        (
            run_jobs(**{"build-x86_64": "failure", "callback": "failure"}),
            "failure",
            PipelineStatus.FAILED,
        ),
        (
            run_jobs(**{"validate-manifest": "failure", "build-x86_64": "skipped"}),
            "failure",
            PipelineStatus.FAILED,
        ),
        (
            run_jobs(**{"build-x86_64": "cancelled", "callback": "failure"}),
            "cancelled",
            PipelineStatus.CANCELLED,
        ),
    ],
)
async def test_reconcile_run_ignores_the_callback_job_when_deriving_status(
    db_session_maker,
    connector,
    flat_manager,
    notifier,
    jobs_payload,
    status,
    pipeline_status,
):
    pipeline_id = await add_pipeline(db_session_maker)
    async with db_session_maker() as db:
        (await db.get(Pipeline, pipeline_id)).provider_data = {
            "owner": "flathub-infra",
            "repo": "vorarbeiter",
            "run_id": "777",
        }
        await db.commit()

    with (
        patch(
            "app.services.github_actions.GitHubActionsService.get_workflow_run_jobs",
            AsyncMock(return_value=jobs_payload),
        ),
        patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled",
            AsyncMock(return_value=False),
        ),
    ):
        await run_reconciler.reconcile_run(pipeline_id, "failure")

    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline_id)).status == pipeline_status
    (finished,) = jobs(connector, "app.tasks.build_finished")
    assert finished["args"]["status_value"] == status


@pytest.mark.asyncio
async def test_reconcile_run_without_job_list_fails_instead_of_guessing(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker)
    async with db_session_maker() as db:
        (await db.get(Pipeline, pipeline_id)).provider_data = {
            "owner": "flathub-infra",
            "repo": "vorarbeiter",
            "run_id": "777",
        }
        await db.commit()

    with (
        patch(
            "app.services.github_actions.GitHubActionsService.get_workflow_run_jobs",
            AsyncMock(return_value=None),
        ),
        pytest.raises(run_reconciler.RunJobsUnavailable),
    ):
        await run_reconciler.reconcile_run(pipeline_id, "failure")

    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline_id)).status == PipelineStatus.RUNNING


@pytest.mark.asyncio
async def test_workflow_run_for_finished_pipeline_does_nothing(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker, status=PipelineStatus.SUCCEEDED)
    async with db_session_maker() as db:
        (await db.get(Pipeline, pipeline_id)).provider_data = {"run_id": "888"}
        await db.commit()

    assert (
        await run_reconciler.enqueue_run_reconciliation(workflow_run_payload(888))
        is None
    )
    await run_reconciler.reconcile_run(pipeline_id, "success")

    assert not connector.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {**workflow_run_payload(1), "action": "requested"},
        workflow_run_payload(1, repo="flathub/other"),
    ],
)
async def test_workflow_run_ignores_other_events(
    db_session_maker, connector, flat_manager, notifier, payload
):
    pipeline_id = await add_pipeline(db_session_maker)
    async with db_session_maker() as db:
        (await db.get(Pipeline, pipeline_id)).provider_data = {"run_id": "1"}
        await db.commit()

    assert await run_reconciler.enqueue_run_reconciliation(payload) is None
    assert not connector.jobs


def test_workflow_run_event_header_routes_to_reconciliation(client: TestClient):
    pipeline_id = uuid.uuid4()
    payload = {**workflow_run_payload(1), "sender": {"login": "github-actions"}}
    body = json.dumps(payload).encode()
    signature = hmac.new(
        settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    with patch(
        "app.routes.webhooks.enqueue_run_reconciliation",
        AsyncMock(return_value=pipeline_id),
    ) as enqueue:
        response = client.post(
            "/api/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-GitHub-Event": "workflow_run",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    assert response.status_code == 202
    assert response.json()["pipeline_id"] == str(pipeline_id)
    enqueue.assert_awaited_once_with(payload)


class WorkerDied(BaseException):
    pass


async def cancelled_pipeline(db_session_maker, repo) -> uuid.UUID:
    pipeline_id = await add_pipeline(db_session_maker, status=PipelineStatus.CANCELLED)
    async with db_session_maker() as db:
        row = await db.get(Pipeline, pipeline_id)
        row.flat_manager_repo = repo
        row.params = {"repo": "flathub/org.test.App", "pr_number": "7"}
        await db.commit()
    return pipeline_id


def retrying_build(db_session_maker, start_pipeline):
    async def create(app_id, params, actor=None):
        async with db_session_maker() as db:
            row = Pipeline(
                app_id=app_id,
                params=params,
                actor=actor,
                provider_data={},
                status=PipelineStatus.PENDING,
            )
            db.add(row)
            await db.commit()
            return row

    async def prepare(pipeline_id):
        async with db_session_maker() as db:
            return await db.get(Pipeline, pipeline_id)

    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])
    build._should_auto_retry = AsyncMock(return_value=True)
    build.create_pipeline = AsyncMock(side_effect=create)
    build.prepare_pipeline_for_start = AsyncMock(side_effect=prepare)
    build.supersede_conflicting_test_pipelines = AsyncMock()
    build.should_queue_test_build = AsyncMock(return_value=False)
    build.start_pipeline = AsyncMock(side_effect=start_pipeline)
    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("repo", ["stable", "test"])
async def test_replayed_build_finished_creates_one_auto_retry(
    db_session_maker, connector, flat_manager, notifier, repo
):
    pipeline_id = await cancelled_pipeline(db_session_maker, repo)

    async def start(pipeline_id):
        async with db_session_maker() as db:
            row = await db.get(Pipeline, pipeline_id)
            row.status = PipelineStatus.RUNNING
            await db.commit()

    build = retrying_build(db_session_maker, start)

    await build.finish_build(pipeline_id, "cancelled", True)
    await build.finish_build(pipeline_id, "cancelled", True)

    build.create_pipeline.assert_awaited_once()
    build.start_pipeline.assert_awaited_once()
    assert notifier.handle_build_completion.await_count == (repo == "test")


@pytest.mark.asyncio
@pytest.mark.parametrize("repo", ["stable", "test"])
async def test_replay_after_worker_death_still_retries(
    db_session_maker, connector, flat_manager, notifier, repo
):
    original_id = await cancelled_pipeline(db_session_maker, repo)
    outcomes = [WorkerDied(), None]

    async def start(pipeline_id):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        async with db_session_maker() as db:
            row = await db.get(Pipeline, pipeline_id)
            row.status = PipelineStatus.RUNNING
            await db.commit()

    build = retrying_build(db_session_maker, start)

    with pytest.raises(WorkerDied):
        await build.finish_build(original_id, "cancelled", True)
    await build.finish_build(original_id, "cancelled", True)

    build.create_pipeline.assert_awaited_once()
    assert build.start_pipeline.await_count == 2
    async with db_session_maker() as db:
        retries = list(
            await db.scalars(
                select(Pipeline).where(
                    Pipeline.params["auto_retry_of"].as_string() == str(original_id)
                )
            )
        )
    assert [r.status for r in retries] == [PipelineStatus.RUNNING]


@pytest.mark.asyncio
async def test_reconcile_run_does_not_guess_a_reprocheck_result(
    db_session_maker, connector, flat_manager, notifier
):
    pipeline_id = await add_pipeline(db_session_maker)
    async with db_session_maker() as db:
        row = await db.get(Pipeline, pipeline_id)
        row.params = {"workflow_id": "reprocheck.yml"}
        await db.commit()

    with patch(
        "app.services.github_actions.GitHubActionsService.get_workflow_run_jobs",
        AsyncMock(),
    ) as get_jobs:
        await run_reconciler.reconcile_run(pipeline_id, "failure")

    get_jobs.assert_not_awaited()
    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline_id)).status == PipelineStatus.RUNNING
