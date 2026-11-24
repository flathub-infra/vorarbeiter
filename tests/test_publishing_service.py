import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2 as httpx
import pytest

from app.models import Pipeline, PipelineStatus
from app.services.publishing import PublishingService, PublishNotReady, PublishResult


@pytest.fixture
def publishing_service():
    return PublishingService()


@pytest.fixture
def mock_pipelines():
    now = datetime.now(UTC)
    return [
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App1",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="stable",
            build_id=1,
            started_at=now,
            params={},
        ),
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App1",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="stable",
            build_id=2,
            started_at=now - timedelta(hours=1),
            params={},
        ),
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App2",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="beta",
            build_id=3,
            started_at=now,
            params={},
        ),
    ]


@pytest.mark.asyncio
async def test_handle_superseded_pipelines(publishing_service, mock_pipelines):
    result = PublishResult()
    duplicates = mock_pipelines[1:2]

    with patch.object(publishing_service.flat_manager, "purge") as mock_purge:
        await publishing_service._handle_superseded_pipelines(duplicates, result)

        assert len(result.superseded) == 1
        assert duplicates[0].status == PipelineStatus.SUPERSEDED
        mock_purge.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_handle_superseded_pipelines_purge_error(
    publishing_service, mock_pipelines
):
    result = PublishResult()
    duplicates = mock_pipelines[1:2]

    with patch.object(publishing_service.flat_manager, "purge") as mock_purge:
        mock_purge.side_effect = httpx.HTTPStatusError(
            "Error",
            request=MagicMock(),
            response=MagicMock(status_code=404, text="Not found"),
        )

        await publishing_service._handle_superseded_pipelines(duplicates, result)

        assert len(result.superseded) == 1
        assert duplicates[0].status == PipelineStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_process_candidate_pipeline_no_build_id(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        flat_manager_repo="stable",
        build_id=None,
        params={},
    )
    result = PublishResult()

    await publishing_service._process_candidate_pipeline(
        pipeline, "org.test.App", "stable", result, datetime.now(UTC)
    )

    assert len(result.errors) == 1
    assert "No build_id available" in result.errors[0]["error"]


@pytest.mark.asyncio
async def test_get_build_info_success(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), build_id=123, params={})

    with patch.object(publishing_service.flat_manager, "get_build_info") as mock_get:
        mock_get.return_value = {
            "build": {
                "repo_state": 2,
                "published_state": 0,
                "commit_job_id": 123,
                "publish_job_id": 456,
            }
        }

        result = await publishing_service._get_build_info(pipeline)
        build = result["build"]
        assert build["repo_state"] == 2
        assert build["published_state"] == 0
        mock_get.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_get_build_info_missing_state(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), build_id=123, params={})

    with patch.object(publishing_service.flat_manager, "get_build_info") as mock_get:
        mock_get.return_value = {"build": {"repo_state": 2}}

        with pytest.raises(ValueError) as exc_info:
            await publishing_service._get_build_info(pipeline)

        assert "Missing state information" in str(exc_info.value)


def test_update_job_ids(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(), commit_job_id=None, publish_job_id=None, params={}
    )
    build_data = {"commit_job_id": 123, "publish_job_id": 456}

    publishing_service._update_job_ids(pipeline, build_data)

    assert pipeline.commit_job_id == 123
    assert pipeline.publish_job_id == 456


def test_update_job_ids_partial(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(), commit_job_id=789, publish_job_id=None, params={}
    )
    build_data = {"commit_job_id": 123, "publish_job_id": 456}

    publishing_service._update_job_ids(pipeline, build_data)

    assert pipeline.commit_job_id == 789
    assert pipeline.publish_job_id == 456


@pytest.mark.asyncio
async def test_handle_build_state_already_published(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.SUCCEEDED,
        build_id=123,
        params={},
    )
    build_info = {
        "build": {"published_state": 2, "repo_state": 2},
        "checks": [],
    }
    result = PublishResult()
    now = datetime.now(UTC)

    await publishing_service._handle_build_state(pipeline, build_info, result, now)

    assert pipeline.status == PipelineStatus.PUBLISHED
    assert pipeline.published_at == now
    assert len(result.published) == 1
    assert str(pipeline.id) in result.published


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stable", "beta"])
async def test_handle_build_state_failed_creates_validation_issue(
    publishing_service, channel
):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        build_id=123,
        flat_manager_repo=channel,
        log_url="https://example.com/logs/123",
        params={"repo": "flathub/org.test.App", "sha": "b" * 40},
    )
    checks = [
        {
            "check_name": "flathub-hooks",
            "build_id": 275210,
            "job_id": 527126,
            "status": 3,
            "status_reason": "One or more validations failed.",
            "results": "{}",
        }
    ]
    build_info = {
        "build": {
            "published_state": 0,
            "repo_state": 3,
            "repo_state_reason": "1 out of 1 checks failed (flathub-hooks)",
        },
        "checks": checks,
    }
    result = PublishResult()
    now = datetime.now(UTC)
    issue_url = "https://github.com/flathub/org.test.App/issues/2"

    with patch(
        "app.services.github_notifier.create_github_issue",
        AsyncMock(return_value=(issue_url, 2)),
    ) as create_issue:
        await publishing_service._handle_build_state(pipeline, build_info, result, now)

    assert pipeline.status == PipelineStatus.FAILED
    assert pipeline.finished_at == now
    assert len(result.errors) == 1
    assert "repo_state FAILED" in result.errors[0]["error"]
    create_issue.assert_awaited_once()
    assert create_issue.await_args is not None
    assert create_issue.await_args.kwargs["git_repo"] == "flathub/org.test.App"
    assert create_issue.await_args.kwargs["title"] == (
        f"{channel.capitalize()} publish validation failed for org.test.App"
    )
    assert f"in the {channel} repository" in create_issue.await_args.kwargs["body"]
    assert pipeline.failure_issue_url == issue_url


@pytest.mark.asyncio
async def test_create_validation_failure_issue_swallowed(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,
        build_id=123,
        flat_manager_repo="stable",
        params={"repo": "flathub/org.test.App"},
    )

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier = MagicMock()
        mock_notifier.create_validation_failure_issue = AsyncMock(
            side_effect=Exception("boom")
        )
        mock_notifier_class.return_value = mock_notifier

        await publishing_service._create_validation_failure_issue(
            pipeline, "validation failed", None
        )


@pytest.mark.asyncio
async def test_handle_build_state_uploading(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )
    build_info = {
        "build": {"published_state": 0, "repo_state": 0},
        "checks": [],
    }
    result = PublishResult()

    await publishing_service._handle_build_state(
        pipeline, build_info, result, datetime.now(UTC)
    )

    # Should skip processing since repo_state is 0 (Uploading)
    assert len(result.published) == 0
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_handle_build_state_ready(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )
    build_info = {
        "build": {"published_state": 0, "repo_state": 2},
        "checks": [],
    }
    result = PublishResult()
    now = datetime.now(UTC)

    with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
        await publishing_service._handle_build_state(pipeline, build_info, result, now)

        mock_publish.assert_called_once_with(123)
        assert pipeline.status == PipelineStatus.COMMITTED  # Should remain COMMITTED
        assert len(result.published) == 1


@pytest.mark.asyncio
async def test_handle_build_state_processing(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )

    for repo_state in [0, 1, 6]:
        build_info = {
            "build": {"published_state": 0, "repo_state": repo_state},
            "checks": [],
        }
        result = PublishResult()

        await publishing_service._handle_build_state(
            pipeline, build_info, result, datetime.now(UTC)
        )

        assert len(result.published) == 0
        assert len(result.errors) == 0
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_handle_build_state_tracks_moderation_review(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )
    held = {
        "build": {"published_state": 0, "repo_state": 6},
        "checks": [{"check_name": "flathub-hooks", "status": 4}],
    }

    await publishing_service._handle_build_state(
        pipeline, held, PublishResult(), datetime.now(UTC)
    )

    assert pipeline.review_required is True
    assert pipeline.status == PipelineStatus.COMMITTED

    approved = {
        "build": {"published_state": 0, "repo_state": 2},
        "checks": [{"check_name": "flathub-hooks", "status": 1}],
    }
    with patch.object(publishing_service, "_try_publish_build") as mock_publish:
        await publishing_service._handle_build_state(
            pipeline, approved, PublishResult(), datetime.now(UTC)
        )

    assert pipeline.review_required is False
    mock_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_publish_build_error(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        build_id=123,
        status=PipelineStatus.COMMITTED,
        params={},
    )
    result = PublishResult()

    with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
        mock_publish.side_effect = httpx.HTTPStatusError(
            "Error",
            request=MagicMock(),
            response=MagicMock(status_code=400, text="Bad request"),
        )

        await publishing_service._try_publish_build(pipeline, result, datetime.now(UTC))

        assert len(result.errors) == 1
        assert "HTTP 400" in result.errors[0]["error"]
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_try_publish_build_success(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        build_id=123,
        status=PipelineStatus.COMMITTED,
        params={},
    )
    result = PublishResult()
    now = datetime.now(UTC)

    with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
        await publishing_service._try_publish_build(pipeline, result, now)

        mock_publish.assert_called_once_with(123)
        assert pipeline.status == PipelineStatus.COMMITTED  # Should remain COMMITTED
        assert len(result.published) == 1
        assert str(pipeline.id) in result.published


def test_handle_build_error_request_error(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = httpx.RequestError("Network error")

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "communication error" in result.errors[0]["error"]


def test_handle_build_error_http_status_error(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = httpx.HTTPStatusError(
        "Error", request=MagicMock(), response=MagicMock(status_code=500)
    )

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "API error: 500" in result.errors[0]["error"]


def test_handle_build_error_value_error(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = ValueError("Invalid response")

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "response error" in result.errors[0]["error"]


def test_handle_build_error_generic(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = Exception("Something went wrong")

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "Unexpected error" in result.errors[0]["error"]


@pytest.fixture
def publish_db(db_session_maker):
    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db
            await db.commit()

    with (
        patch("app.services.publishing.get_db", get_test_db),
        patch(
            "app.pipelines.build.BuildPipeline.fetch_and_store_job_id",
            AsyncMock(),
        ),
        patch("app.services.github_notifier.GitHubNotifier"),
    ):
        yield db_session_maker


async def add_committed(db_session_maker, count: int) -> list[Pipeline]:
    now = datetime.now(UTC)
    pipelines = [
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="stable",
            build_id=n + 1,
            started_at=now - timedelta(hours=n),
            params={},
        )
        for n in range(count)
    ]
    async with db_session_maker() as db:
        db.add_all(pipelines)
        await db.commit()
    return pipelines


async def status_of(db_session_maker, pipeline: Pipeline) -> PipelineStatus:
    async with db_session_maker() as db:
        return (await db.get(Pipeline, pipeline.id)).status


def build_info(repo_state: int) -> dict:
    return {"build": {"repo_state": repo_state, "published_state": 0}, "checks": []}


@pytest.mark.asyncio
async def test_publish_pipeline_publishes_newest_and_supersedes_rest(
    publishing_service, publish_db
):
    newest, older = await add_committed(publish_db, 2)

    with (
        patch.object(
            publishing_service.flat_manager,
            "get_build_info",
            AsyncMock(return_value=build_info(2)),
        ),
        patch.object(publishing_service.flat_manager, "publish") as publish,
        patch.object(publishing_service.flat_manager, "purge") as purge,
    ):
        await publishing_service.publish_pipeline("org.test.App", "stable")

    publish.assert_awaited_once_with(newest.build_id)
    purge.assert_awaited_once_with(older.build_id)
    assert await status_of(publish_db, older) == PipelineStatus.SUPERSEDED
    assert await status_of(publish_db, newest) == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_publish_pipeline_not_ready_retries_and_keeps_supersede(
    publishing_service, publish_db
):
    newest, older = await add_committed(publish_db, 2)

    with (
        patch.object(
            publishing_service.flat_manager,
            "get_build_info",
            AsyncMock(return_value=build_info(6)),
        ),
        patch.object(publishing_service.flat_manager, "publish") as publish,
        patch.object(publishing_service.flat_manager, "purge"),
        pytest.raises(PublishNotReady),
    ):
        await publishing_service.publish_pipeline("org.test.App", "stable")

    publish.assert_not_called()
    assert await status_of(publish_db, older) == PipelineStatus.SUPERSEDED
    assert await status_of(publish_db, newest) == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_publish_pipeline_validation_failure_does_not_retry(
    publishing_service, publish_db
):
    [pipeline] = await add_committed(publish_db, 1)

    with patch.object(
        publishing_service.flat_manager,
        "get_build_info",
        AsyncMock(return_value=build_info(3)),
    ):
        await publishing_service.publish_pipeline("org.test.App", "stable")

    assert await status_of(publish_db, pipeline) == PipelineStatus.FAILED


@pytest.mark.asyncio
async def test_publish_pipeline_transport_error_retries(publishing_service, publish_db):
    await add_committed(publish_db, 1)

    with (
        patch.object(
            publishing_service.flat_manager,
            "get_build_info",
            AsyncMock(side_effect=httpx.ConnectError("down")),
        ),
        pytest.raises(PublishNotReady),
    ):
        await publishing_service.publish_pipeline("org.test.App", "stable")


@pytest.mark.asyncio
async def test_publish_pipeline_under_review_waits_for_the_next_event(
    publishing_service, publish_db
):
    [pipeline] = await add_committed(publish_db, 1)
    info = build_info(6)
    info["checks"] = [{"status": 4}]

    with (
        patch.object(
            publishing_service.flat_manager,
            "get_build_info",
            AsyncMock(return_value=info),
        ),
        patch.object(publishing_service.flat_manager, "publish") as publish,
    ):
        await publishing_service.publish_pipeline("org.test.App", "stable")

    publish.assert_not_called()
    async with publish_db() as db:
        row = await db.get(Pipeline, pipeline.id)
        assert row.review_required is True
        assert row.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_publish_pipeline_leaves_tracked_publish_to_job_events(
    publishing_service, publish_db
):
    [pipeline] = await add_committed(publish_db, 1)
    async with publish_db() as db:
        (await db.get(Pipeline, pipeline.id)).publish_job_id = 456
        await db.commit()
    info = {"build": {"repo_state": 2, "published_state": 2}, "checks": []}

    with (
        patch.object(
            publishing_service.flat_manager,
            "get_build_info",
            AsyncMock(return_value=info),
        ),
        patch.object(publishing_service.flat_manager, "publish") as publish,
    ):
        await publishing_service.publish_pipeline("org.test.App", "stable")

    publish.assert_not_called()
    async with publish_db() as db:
        row = await db.get(Pipeline, pipeline.id)
        assert row.status == PipelineStatus.COMMITTED
        assert row.published_at is None
