import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines.build import BuildPipeline
from app.services import GitHubActionsService
from tests.conftest import create_mock_get_db


@pytest.fixture
def mock_provider():
    provider = AsyncMock(spec=GitHubActionsService)
    return provider


@pytest.fixture
def build_pipeline(mock_provider):
    with patch("app.services.github_actions_service", mock_provider):
        pipeline = BuildPipeline()
        pipeline.start_pending_builds = AsyncMock(return_value=[])  # ty: ignore[invalid-assignment]
        return pipeline


@pytest.fixture
def sample_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.flathub.Test",
        status=PipelineStatus.PENDING,
        params={"branch": "main"},
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
        callback_token="test_token_12345",
    )


@pytest.mark.asyncio
async def test_create_pipeline(build_pipeline, mock_db, monkeypatch):
    app_id = "org.flathub.Test"
    params = {"branch": "main"}

    mock_db.flush = AsyncMock()

    test_pipeline = MagicMock(spec=Pipeline)
    test_pipeline.id = uuid.uuid4()
    test_pipeline.app_id = app_id
    test_pipeline.params = params
    test_pipeline.status = PipelineStatus.PENDING

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch("app.pipelines.build.Pipeline", return_value=test_pipeline):
            result = await build_pipeline.create_pipeline(app_id, params)

    assert mock_db.add.called
    assert mock_db.flush.called
    assert result.app_id == app_id
    assert result.params == params
    assert result.status == PipelineStatus.PENDING


@pytest.mark.asyncio
async def test_start_pipeline(build_pipeline, mock_db):
    pipeline_id = uuid.uuid4()

    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id
    mock_pipeline.status = PipelineStatus.PENDING
    mock_pipeline.app_id = "org.flathub.Test"
    mock_pipeline.params = {"repo": "test", "branch": "main"}

    async def mock_get(model_class, model_id):
        if model_class is Pipeline and model_id == pipeline_id:
            return mock_pipeline
        return None

    mock_db.get = AsyncMock(side_effect=mock_get)

    mock_get_db = create_mock_get_db(mock_db)

    dispatch_result = {"status": "dispatched"}
    build_pipeline.provider.dispatch = AsyncMock(return_value=dispatch_result)

    mock_httpx_response = MagicMock()
    mock_httpx_response.raise_for_status = MagicMock()
    mock_httpx_response.json.return_value = {"id": 12345, "token": "test-token"}

    mock_httpx_client = AsyncMock()
    mock_httpx_client.__aenter__.return_value.post.return_value = mock_httpx_response

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            with patch("app.pipelines.build.get_app_p90_build_time", return_value=None):
                result = await build_pipeline.start_pipeline(pipeline_id)

    assert result.status == PipelineStatus.RUNNING
    assert build_pipeline.provider.dispatch.called
    assert mock_httpx_client.__aenter__.return_value.post.call_count == 2

    dispatch_call_args = build_pipeline.provider.dispatch.call_args[0]
    job_data = dispatch_call_args[2]
    assert job_data["params"]["inputs"]["flat_manager_token"] == "test-token"


@pytest.mark.asyncio
@patch("app.pipelines.build.get_app_p90_build_time", return_value=None)
@patch("app.pipelines.build.get_db")
@patch("app.services.github_actions_service")
@patch("httpx.AsyncClient")
@pytest.mark.parametrize(
    "source_branch, expected_branch, expected_flat_manager_repo",
    [
        ("master", "stable", "test"),
        ("beta", "beta", "test"),
        ("feature/new-thing", "test", "test"),
        (None, "test", "test"),
        ("branch/my-feature", "my-feature", "test"),
    ],
)
async def test_start_pipeline_branch_mapping(
    mock_httpx_client,
    mock_github_provider,
    mock_get_db,
    mock_p90,
    source_branch,
    expected_branch,
    expected_flat_manager_repo,
):
    """
    Verify that start_pipeline correctly maps the source branch (from params)
    to the target branch used in the GitHub dispatch inputs.
    """
    pipeline_id = uuid.uuid4()
    app_id = "test.app"
    params = {"branch": source_branch} if source_branch else {}

    mock_db_session = AsyncMock()
    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id=app_id,
        params=params,
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )
    mock_db_session.get.return_value = mock_pipeline
    mock_get_db.return_value.__aenter__.return_value = mock_db_session

    # Make the provider a proper AsyncMock
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 12345, "token": "test-token"}

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock(return_value=mock_response)
    mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

    build_pipeline = BuildPipeline()
    build_pipeline.provider = mock_github_provider

    await build_pipeline.start_pipeline(pipeline_id)

    mock_db_session.get.assert_called_once_with(Pipeline, pipeline_id)
    assert mock_pipeline.status == PipelineStatus.RUNNING
    assert mock_pipeline.started_at is not None

    assert mock_httpx_instance.post.call_count == 2
    first_call_args = mock_httpx_instance.post.call_args_list[0]
    post_url = first_call_args[0][0]
    post_data = first_call_args[1]["json"]
    assert "build" in post_url
    assert post_data["repo"] == expected_flat_manager_repo

    second_call_args = mock_httpx_instance.post.call_args_list[1]
    token_url = second_call_args[0][0]
    token_data = second_call_args[1]["json"]
    assert "token_subset" in token_url
    assert token_data["name"] == "upload"
    assert token_data["scope"] == ["upload"]
    assert token_data["prefix"] == [app_id]

    mock_github_provider.dispatch.assert_awaited_once()
    call_args, call_kwargs = mock_github_provider.dispatch.call_args
    dispatched_job_data = call_args[2]
    assert (
        dispatched_job_data["params"]["inputs"]["flat_manager_repo"]
        == expected_flat_manager_repo
    )
    assert mock_pipeline.provider_data == {"dispatch_result": "ok"}

    mock_db_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_handle_status_callback_success(build_pipeline, mock_db, sample_pipeline):
    mock_db.get.return_value = sample_pipeline

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        pipeline, updates = await build_pipeline.handle_status_callback(
            sample_pipeline.id, {"status": "success"}
        )

    assert pipeline.status == PipelineStatus.SUCCEEDED
    assert pipeline.finished_at is not None
    build_pipeline.start_pending_builds.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_status_callback_success_drains_oldest_pending_pipeline():
    completed_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.flathub.Completed",
        status=PipelineStatus.RUNNING,
        params={"workflow_id": "build.yml"},
        created_at=datetime.now(),
        provider_data={},
        callback_token="callback-token",
    )
    oldest_pending_id = uuid.uuid4()
    newer_pending_id = uuid.uuid4()

    callback_db = AsyncMock(spec=AsyncSession)
    callback_db.get.return_value = completed_pipeline

    pending_result = MagicMock()
    pending_result.fetchall.return_value = [
        (oldest_pending_id,),
        (newer_pending_id,),
    ]

    pending_db = AsyncMock(spec=AsyncSession)
    pending_db.execute = AsyncMock(return_value=pending_result)

    db_sessions = [callback_db, pending_db]

    @asynccontextmanager
    async def sequenced_get_db(*, use_replica: bool = False):
        if not db_sessions:
            raise AssertionError("Unexpected get_db() call")
        yield db_sessions.pop(0)

    build_pipeline = BuildPipeline()
    notifier = MagicMock()
    notifier.handle_build_completion = AsyncMock()

    async def _start_pipeline(pipeline_id):
        started_pipeline = MagicMock(spec=Pipeline)
        started_pipeline.id = pipeline_id
        return started_pipeline

    build_pipeline.start_pipeline = AsyncMock(side_effect=_start_pipeline)  # ty: ignore[invalid-assignment]

    with (
        patch("app.pipelines.build.get_db", sequenced_get_db),
        patch("app.pipelines.build.settings.max_concurrent_builds", 0),
        patch("app.pipelines.build.GitHubNotifier", return_value=notifier),
    ):
        pipeline, updates = await build_pipeline.handle_status_callback(
            completed_pipeline.id, {"status": "success"}
        )

    assert pipeline.status == PipelineStatus.SUCCEEDED
    assert updates["pipeline_status"] == "success"
    started_ids = [
        call.args[0]
        for call in build_pipeline.start_pipeline.await_args_list  # ty: ignore[unresolved-attribute]
    ]
    assert started_ids == [oldest_pending_id, newer_pending_id]
    pending_query = str(pending_db.execute.await_args.args[0])  # ty: ignore[unresolved-attribute]
    assert "ORDER BY created_at ASC" in pending_query
    assert not db_sessions


@pytest.mark.asyncio
async def test_handle_status_callback_failure(build_pipeline, mock_db, sample_pipeline):
    mock_db.get.return_value = sample_pipeline

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.return_value = False
            pipeline, updates = await build_pipeline.handle_status_callback(
                sample_pipeline.id, {"status": "failure"}
            )

    assert pipeline.status == PipelineStatus.FAILED
    assert pipeline.finished_at is not None


@pytest.mark.asyncio
async def test_handle_status_callback_failure_reclassified_as_cancelled(
    build_pipeline, mock_db, sample_pipeline
):
    """Test that a 'failure' callback gets reclassified as 'cancelled' when spot instance termination is detected."""
    mock_db.get.return_value = sample_pipeline

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.return_value = True  # Simulate cancellation detected
            pipeline, updates = await build_pipeline.handle_status_callback(
                sample_pipeline.id, {"status": "failure"}
            )

    assert pipeline.status == PipelineStatus.CANCELLED
    assert pipeline.finished_at is not None
    assert updates["pipeline_status"] == "cancelled"


@pytest.mark.asyncio
async def test_handle_status_callback_failure_cancellation_check_error(
    build_pipeline, mock_db, sample_pipeline
):
    """Test that if cancellation check fails, the build is still marked as failed."""
    mock_db.get.return_value = sample_pipeline

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.side_effect = Exception("API error")
            pipeline, updates = await build_pipeline.handle_status_callback(
                sample_pipeline.id, {"status": "failure"}
            )

    assert pipeline.status == PipelineStatus.FAILED
    assert pipeline.finished_at is not None
    assert updates["pipeline_status"] == "failure"


@pytest.fixture
def mock_get_db(mock_db):
    @asynccontextmanager
    async def _mock_get_db(*, use_replica: bool = False):
        yield mock_db

    with patch("app.routes.pipelines.get_db", _mock_get_db):
        yield mock_db


@pytest.fixture
def mock_pipeline_service():
    with patch("app.routes.pipelines.pipeline_service") as service_mock:
        service_mock.trigger_manual_pipeline = AsyncMock(
            return_value={
                "status": "created",
                "pipeline_id": str(uuid.uuid4()),
                "app_id": "org.flathub.Test",
                "pipeline_status": "running",
            }
        )
        yield service_mock


def test_trigger_pipeline_endpoint(mock_pipeline_service):
    from app.config import settings

    test_client = TestClient(app)

    request_data = {
        "app_id": "org.flathub.Test",
        "params": {"branch": "main"},
    }

    headers = {"Authorization": f"Bearer {settings.admin_token}"}

    response = test_client.post("/api/pipelines", json=request_data, headers=headers)

    assert response.status_code == 201
    assert "pipeline_id" in response.json()
    assert response.json()["app_id"] == "org.flathub.Test"
    assert response.json()["status"] == "created"
    assert response.json()["pipeline_status"] == "running"

    mock_pipeline_service.trigger_manual_pipeline.assert_called_once_with(
        app_id="org.flathub.Test",
        params={"branch": "main"},
    )


def test_trigger_pipeline_unauthorized(mock_pipeline_service):
    test_client = TestClient(app)

    request_data = {
        "app_id": "org.flathub.Test",
        "params": {"branch": "main"},
    }

    # Test with no token
    response = test_client.post("/api/pipelines", json=request_data)
    assert response.status_code == 401  # Missing Authorization header

    # Test with invalid token
    headers = {"Authorization": "Bearer invalid-token"}
    response = test_client.post("/api/pipelines", json=request_data, headers=headers)
    assert response.status_code == 401
    assert "Invalid API token" in response.json()["detail"]


def test_list_pipelines_endpoint(mock_get_db):
    test_client = TestClient(app)

    pipelines = [
        MagicMock(
            id=uuid.uuid4(),
            app_id="org.flathub.Test1",
            status=PipelineStatus.RUNNING,
            flat_manager_repo="stable",
            triggered_by=PipelineTrigger.MANUAL,
            build_id=123,
            created_at=datetime.now(),
            started_at=datetime.now(),
            finished_at=None,
            published_at=None,
            repro_pipeline_id=None,
        ),
        MagicMock(
            id=uuid.uuid4(),
            app_id="org.flathub.Test2",
            status=PipelineStatus.SUCCEEDED,
            flat_manager_repo="beta",
            triggered_by=PipelineTrigger.WEBHOOK,
            build_id=456,
            created_at=datetime.now(),
            started_at=datetime.now(),
            finished_at=datetime.now(),
            published_at=None,
            repro_pipeline_id=uuid.uuid4(),
        ),
    ]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = pipelines
    mock_get_db.execute.return_value = mock_result

    response = test_client.get("/api/pipelines")

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert response.json()[0]["app_id"] == "org.flathub.Test1"
    assert response.json()[0]["status"] == "running"
    assert response.json()[0]["triggered_by"] == "manual"
    assert "build_id" in response.json()[0]
    assert response.json()[1]["app_id"] == "org.flathub.Test2"
    assert response.json()[1]["status"] == "succeeded"
    assert response.json()[1]["triggered_by"] == "webhook"
    assert "build_id" in response.json()[1]


def test_get_pipeline_endpoint(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db.get.return_value = sample_pipeline

    response = test_client.get(f"/api/pipelines/{pipeline_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(pipeline_id)
    assert response.json()["app_id"] == sample_pipeline.app_id
    assert response.json()["status"] == sample_pipeline.status.value


def test_get_pipeline_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()

    mock_get_db.get.return_value = None

    response = test_client.get(f"/api/pipelines/{pipeline_id}")

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]


def test_redirect_to_log_url(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    sample_pipeline.log_url = "https://example.com/logs/12345"

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with patch("app.routes.pipelines.get_db", mock_get_db_session):
        mock_get_db.get.return_value = sample_pipeline

        response = test_client.get(
            f"/api/pipelines/{pipeline_id}/log_url", follow_redirects=False
        )

    assert response.status_code == 307
    assert response.headers["Location"] == "https://example.com/logs/12345"


def test_redirect_to_log_url_not_available(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    sample_pipeline.log_url = None

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with patch("app.routes.pipelines.get_db", mock_get_db_session):
        mock_get_db.get.return_value = sample_pipeline

        response = test_client.get(f"/api/pipelines/{pipeline_id}/log_url")

    assert response.status_code == 202
    assert "Retry-After" in response.headers
    assert "Log URL not available yet" in response.json()["detail"]


def test_redirect_to_log_url_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with patch("app.routes.pipelines.get_db", mock_get_db_session):
        mock_get_db.get.return_value = None

        response = test_client.get(f"/api/pipelines/{pipeline_id}/log_url")

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_start_pipeline_stores_default_build_type():
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.example.app",
        params={"repo": "test", "branch": "main"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 12345, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    with patch("app.pipelines.build.get_db") as mock_get_db:
        with patch("httpx.AsyncClient") as mock_httpx_client:
            with patch("app.pipelines.build.get_app_p90_build_time") as mock_p90:
                mock_p90.return_value = None
                mock_get_db.return_value.__aenter__.return_value = mock_db_session
                mock_httpx_client.return_value.__aenter__.return_value = (
                    mock_httpx_instance
                )

                build_pipeline = BuildPipeline()
                build_pipeline.provider = mock_github_provider

                await build_pipeline.start_pipeline(pipeline_id)

                assert mock_pipeline.params["build_type"] == "medium"
                mock_p90.assert_called_once_with(mock_db_session, "org.example.app")


@pytest.mark.asyncio
async def test_start_pipeline_stores_hardcoded_build_type():
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.chromium.Chromium",
        params={"repo": "test", "branch": "main"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 12345, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    with patch("app.pipelines.build.get_db") as mock_get_db:
        with patch("httpx.AsyncClient") as mock_httpx_client:
            mock_get_db.return_value.__aenter__.return_value = mock_db_session
            mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

            build_pipeline = BuildPipeline()
            build_pipeline.provider = mock_github_provider

            await build_pipeline.start_pipeline(pipeline_id)

            assert mock_pipeline.params["build_type"] == "large"


@pytest.mark.asyncio
async def test_start_pipeline_stores_parameter_build_type():
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.example.app",
        params={"repo": "test", "branch": "main", "build_type": "medium"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 12345, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    with patch("app.pipelines.build.get_db") as mock_get_db:
        with patch("httpx.AsyncClient") as mock_httpx_client:
            with patch("app.pipelines.build.get_app_p90_build_time") as mock_p90:
                mock_p90.return_value = None
                mock_get_db.return_value.__aenter__.return_value = mock_db_session
                mock_httpx_client.return_value.__aenter__.return_value = (
                    mock_httpx_instance
                )

                build_pipeline = BuildPipeline()
                build_pipeline.provider = mock_github_provider

                await build_pipeline.start_pipeline(pipeline_id)

                assert mock_pipeline.params["build_type"] == "medium"


@pytest.mark.asyncio
async def test_start_pipeline_reuses_stored_metadata():
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.chromium.Chromium",
        params={"repo": "test", "branch": "main", "build_type": "medium"},
        status=PipelineStatus.PENDING,
        flat_manager_repo="test",
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 12345, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    with (
        patch("app.pipelines.build.get_db") as mock_get_db,
        patch("httpx.AsyncClient") as mock_httpx_client,
        patch("app.pipelines.build.determine_build_type") as mock_determine,
        patch("app.pipelines.build.get_flat_manager_repo") as mock_repo_lookup,
    ):
        mock_get_db.return_value.__aenter__.return_value = mock_db_session
        mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

        build_pipeline = BuildPipeline()
        build_pipeline.provider = mock_github_provider

        await build_pipeline.start_pipeline(pipeline_id)

        assert mock_pipeline.params["build_type"] == "medium"
        mock_determine.assert_not_called()
        mock_repo_lookup.assert_not_called()


@pytest.mark.asyncio
@patch("app.pipelines.build.get_app_p90_build_time")
@patch("app.pipelines.build.get_db")
@patch("httpx.AsyncClient")
@pytest.mark.parametrize(
    "p90_value, expected_build_type",
    [
        (8.0, "default"),
        (10.0, "default"),
        (15.0, "default"),
        (15.1, "medium"),
        (None, "medium"),
    ],
)
async def test_start_pipeline_p90_routing(
    mock_httpx_client,
    mock_get_db,
    mock_p90,
    p90_value,
    expected_build_type,
):
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.example.app",
        params={"repo": "test", "branch": "main"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline
    mock_get_db.return_value.__aenter__.return_value = mock_db_session

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 12345, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response
    mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    mock_p90.return_value = p90_value

    build_pipeline = BuildPipeline()
    build_pipeline.provider = mock_github_provider

    await build_pipeline.start_pipeline(pipeline_id)

    assert mock_pipeline.params["build_type"] == expected_build_type
    mock_p90.assert_called_once_with(mock_db_session, "org.example.app")


@pytest.mark.asyncio
async def test_handle_status_callback_auto_retry_stable_cancelled(
    build_pipeline, mock_db, sample_pipeline
):
    """Test that a cancelled stable build is automatically retried once."""
    sample_pipeline.flat_manager_repo = "stable"
    sample_pipeline.params = {"branch": "main"}
    mock_db.get.return_value = sample_pipeline
    mock_db.flush = AsyncMock()

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.return_value = True
            with patch.object(
                build_pipeline, "create_pipeline", new_callable=AsyncMock
            ) as mock_create:
                with patch.object(
                    build_pipeline, "start_pipeline", new_callable=AsyncMock
                ) as mock_start:
                    retry_pipeline = Pipeline(
                        id=uuid.uuid4(),
                        app_id="org.flathub.Test",
                        status=PipelineStatus.PENDING,
                        params={"branch": "main", "auto_retried": True},
                        created_at=datetime.now(),
                        triggered_by=PipelineTrigger.MANUAL,
                        provider_data={},
                        callback_token="test_token",
                    )
                    mock_create.return_value = retry_pipeline
                    mock_start.return_value = retry_pipeline

                    pipeline, updates = await build_pipeline.handle_status_callback(
                        sample_pipeline.id, {"status": "failure"}
                    )

    assert pipeline.status == PipelineStatus.CANCELLED
    mock_create.assert_called_once()
    mock_start.assert_called_once()
    call_args = mock_create.call_args
    assert call_args.kwargs["params"]["auto_retried"] is True


@pytest.mark.asyncio
async def test_handle_status_callback_auto_retry_beta_cancelled(
    build_pipeline, mock_db, sample_pipeline
):
    """Test that a cancelled beta build is automatically retried once."""
    sample_pipeline.flat_manager_repo = "beta"
    sample_pipeline.params = {"branch": "main"}
    mock_db.get.return_value = sample_pipeline
    mock_db.flush = AsyncMock()

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.return_value = True
            with patch.object(
                build_pipeline, "create_pipeline", new_callable=AsyncMock
            ) as mock_create:
                with patch.object(
                    build_pipeline, "start_pipeline", new_callable=AsyncMock
                ) as mock_start:
                    retry_pipeline = Pipeline(
                        id=uuid.uuid4(),
                        app_id="org.flathub.Test",
                        status=PipelineStatus.PENDING,
                        params={"branch": "main", "auto_retried": True},
                        created_at=datetime.now(),
                        triggered_by=PipelineTrigger.MANUAL,
                        provider_data={},
                        callback_token="test_token",
                    )
                    mock_create.return_value = retry_pipeline
                    mock_start.return_value = retry_pipeline

                    pipeline, updates = await build_pipeline.handle_status_callback(
                        sample_pipeline.id, {"status": "failure"}
                    )

    assert pipeline.status == PipelineStatus.CANCELLED
    mock_create.assert_called_once()
    mock_start.assert_called_once()


@pytest.mark.asyncio
async def test_handle_status_callback_no_auto_retry_test_cancelled(
    build_pipeline, mock_db, sample_pipeline
):
    """Test that test builds are NOT automatically retried when cancelled."""
    sample_pipeline.flat_manager_repo = "test"
    sample_pipeline.params = {"branch": "main"}
    mock_db.get.return_value = sample_pipeline
    mock_db.flush = AsyncMock()

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.return_value = True
            with patch.object(
                build_pipeline, "create_pipeline", new_callable=AsyncMock
            ) as mock_create:
                pipeline, updates = await build_pipeline.handle_status_callback(
                    sample_pipeline.id, {"status": "failure"}
                )

    assert pipeline.status == PipelineStatus.CANCELLED
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_handle_status_callback_no_auto_retry_already_retried(
    build_pipeline, mock_db, sample_pipeline
):
    """Test that already-retried builds are NOT retried again."""
    sample_pipeline.flat_manager_repo = "stable"
    sample_pipeline.params = {"branch": "main", "auto_retried": True}
    mock_db.get.return_value = sample_pipeline
    mock_db.flush = AsyncMock()

    mock_get_db = create_mock_get_db(mock_db)

    with patch("app.pipelines.build.get_db", mock_get_db):
        with patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled"
        ) as mock_check_cancelled:
            mock_check_cancelled.return_value = True
            with patch.object(
                build_pipeline, "create_pipeline", new_callable=AsyncMock
            ) as mock_create:
                pipeline, updates = await build_pipeline.handle_status_callback(
                    sample_pipeline.id, {"status": "failure"}
                )

    assert pipeline.status == PipelineStatus.CANCELLED
    mock_create.assert_not_called()


def test_pipeline_metadata_callback_app_id(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    sample_pipeline.app_id = "flathub"

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"app_id": "org.real.AppId"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/metadata",
            json=data,
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["app_id"] == "org.real.AppId"
    assert sample_pipeline.app_id == "org.real.AppId"


def test_pipeline_metadata_callback_end_of_life(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {
            "end_of_life": "This application has been replaced by org.flathub.NewApp.",
            "end_of_life_rebase": "org.flathub.NewApp",
        }
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/metadata",
            json=data,
            headers=headers,
        )

    assert response.status_code == 200
    assert (
        response.json()["end_of_life"]
        == "This application has been replaced by org.flathub.NewApp."
    )
    assert response.json()["end_of_life_rebase"] == "org.flathub.NewApp"
    assert (
        sample_pipeline.end_of_life
        == "This application has been replaced by org.flathub.NewApp."
    )
    assert sample_pipeline.end_of_life_rebase == "org.flathub.NewApp"


def test_pipeline_metadata_callback_invalid_token(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"app_id": "org.real.AppId"}
        headers = {"Authorization": "Bearer wrong_token"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/metadata",
            json=data,
            headers=headers,
        )

    assert response.status_code == 401
    assert "Invalid callback token" in response.json()["detail"]


def test_pipeline_log_url_callback_success(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
        patch("app.pipelines.build.GitHubNotifier") as mock_notifier_class,
    ):
        mock_get_db.get.return_value = sample_pipeline
        mock_notifier = MagicMock()
        mock_notifier.handle_build_started = AsyncMock()
        mock_notifier_class.return_value = mock_notifier

        data = {"log_url": "https://github.com/flathub-infra/builds/runs/12345"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/log_url", json=data, headers=headers
        )

    assert response.status_code == 200
    assert (
        response.json()["log_url"]
        == "https://github.com/flathub-infra/builds/runs/12345"
    )
    assert (
        sample_pipeline.log_url == "https://github.com/flathub-infra/builds/runs/12345"
    )
    mock_notifier.handle_build_started.assert_called_once()


def test_pipeline_log_url_callback_already_set(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    sample_pipeline.log_url = "https://github.com/existing/run/999"

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"log_url": "https://github.com/flathub-infra/builds/runs/12345"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/log_url", json=data, headers=headers
        )

    assert response.status_code == 409
    assert "Log URL already set" in response.json()["detail"]


def test_pipeline_log_url_callback_missing_log_url(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data: dict[str, str] = {}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/log_url", json=data, headers=headers
        )

    assert response.status_code == 400
    assert "log_url is required" in response.json()["detail"]


def test_pipeline_status_callback_success(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    sample_pipeline.status = PipelineStatus.RUNNING
    sample_pipeline.build_id = 123
    sample_pipeline.params = {"sha": "abc123", "repo": "flathub/test-app"}

    mock_flat_manager = MagicMock()
    mock_flat_manager.commit = AsyncMock()

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
        patch("app.pipelines.build.FlatManagerClient") as mock_fm_class,
        patch("app.pipelines.build.GitHubNotifier") as mock_notifier_class,
        patch.object(
            BuildPipeline,
            "start_pending_builds",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_fm_class.return_value = mock_flat_manager
        mock_get_db.get.return_value = sample_pipeline
        mock_notifier = MagicMock()
        mock_notifier.handle_build_completion = AsyncMock()
        mock_notifier_class.return_value = mock_notifier

        data = {"status": "success"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/status", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["pipeline_status"] == "success"
    assert sample_pipeline.status == PipelineStatus.SUCCEEDED
    assert sample_pipeline.finished_at is not None


def test_pipeline_status_callback_already_finalized(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    sample_pipeline.status = PipelineStatus.SUCCEEDED

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"status": "success"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/status", json=data, headers=headers
        )

    assert response.status_code == 409
    assert "Pipeline status already finalized" in response.json()["detail"]


def test_pipeline_status_callback_missing_status(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data: dict[str, str] = {}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/status", json=data, headers=headers
        )

    assert response.status_code == 400
    assert "status is required" in response.json()["detail"]


def test_pipeline_reprocheck_callback_success(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    reprocheck_pipeline_id = uuid.uuid4()
    original_pipeline_id = uuid.uuid4()

    reprocheck_pipeline = Pipeline(
        id=reprocheck_pipeline_id,
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={"workflow_id": "reprocheck.yml"},
        callback_token="reprocheck_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    Pipeline(
        id=original_pipeline_id,
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={},
        callback_token="original_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.get.side_effect = [
        reprocheck_pipeline,
        reprocheck_pipeline,
    ]
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()

    mock_check_result = AsyncMock()
    mock_check_result.first = lambda: (
        str(original_pipeline_id),
        None,
    )  # id, repro_pipeline_id (None means not set)
    mock_db.execute.return_value = mock_check_result

    mock_update_db = AsyncMock(spec=AsyncSession)
    mock_update_db.execute = AsyncMock()
    mock_update_db.commit = AsyncMock()

    mock_get_db_session = create_mock_get_db(mock_db)

    call_count = 0

    def get_db_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:  # First two calls for routes
            return mock_get_db_session()
        else:  # Third call is for the UPDATE transaction in build.py
            return AsyncMock(__aenter__=AsyncMock(return_value=mock_update_db))

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", side_effect=get_db_side_effect),
    ):
        data = {"status": "success", "build_pipeline_id": str(original_pipeline_id)}
        headers = {"Authorization": "Bearer reprocheck_token"}

        response = test_client.post(
            f"/api/pipelines/{reprocheck_pipeline_id}/callback/reprocheck",
            json=data,
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["pipeline_status"] == "success"
    assert reprocheck_pipeline.status == PipelineStatus.SUCCEEDED
    assert mock_db.execute.call_count == 1
    check_call = mock_db.execute.call_args_list[0]
    assert "SELECT id, repro_pipeline_id FROM pipeline" in str(check_call[0][0])
    assert mock_update_db.execute.call_count == 1  # UPDATE
    update_call = mock_update_db.execute.call_args_list[0]
    assert "UPDATE pipeline SET repro_pipeline_id" in str(update_call[0][0])
    mock_update_db.commit.assert_called_once()


def test_pipeline_reprocheck_callback_missing_status(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"build_pipeline_id": str(uuid.uuid4())}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/reprocheck",
            json=data,
            headers=headers,
        )

    assert response.status_code == 400
    assert "status is required" in response.json()["detail"]


def test_pipeline_cost_callback_success(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"cost": 0.0234}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/cost", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["total_cost"] == 0.0234
    assert sample_pipeline.total_cost == 0.0234


def test_pipeline_cost_callback_invalid_token(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"cost": 0.0234}
        headers = {"Authorization": "Bearer wrong_token"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/cost", json=data, headers=headers
        )

    assert response.status_code == 401
    assert "Invalid callback token" in response.json()["detail"]


def test_pipeline_cost_callback_accumulates(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    sample_pipeline.total_cost = 0.01

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"cost": 0.02}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/cost", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["total_cost"] == 0.03
    assert sample_pipeline.total_cost == 0.03


def test_pipeline_cost_callback_missing_cost(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db_session = create_mock_get_db(mock_get_db)

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data: dict[str, str] = {}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback/cost", json=data, headers=headers
        )

    assert response.status_code == 400
    assert "cost is required" in response.json()["detail"]


@pytest.mark.asyncio
async def test_supersedes_running_pipeline_on_start():
    """Test that starting a new pipeline supersedes conflicting RUNNING pipelines."""
    new_pipeline_id = uuid.uuid4()
    old_pipeline_id = uuid.uuid4()

    old_pipeline = Pipeline(
        id=old_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/heads/master"},
        status=PipelineStatus.RUNNING,
        flat_manager_repo="stable",
        build_id=111,
        provider_data={"run_id": "12345"},
        callback_token=str(uuid.uuid4()),
    )

    new_pipeline = Pipeline(
        id=new_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/heads/master"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = new_pipeline

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [old_pipeline]
    mock_db_session.execute.return_value = mock_execute_result

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 99999, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    mock_flat_manager = MagicMock()
    mock_flat_manager.create_build = AsyncMock(return_value={"id": 99999})
    mock_flat_manager.create_token_subset = AsyncMock(return_value="upload-token")
    mock_flat_manager.get_build_url = MagicMock(
        return_value="https://flat-manager/build/99999"
    )
    mock_flat_manager.purge = AsyncMock()

    mock_github_actions_cancel = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        with patch("httpx.AsyncClient") as mock_httpx_client:
            with patch(
                "app.pipelines.build.GitHubActionsService"
            ) as mock_github_actions_class:
                mock_get_db.return_value.__aenter__.return_value = mock_db_session
                mock_httpx_client.return_value.__aenter__.return_value = (
                    mock_httpx_instance
                )
                mock_github_actions_class.return_value.cancel = (
                    mock_github_actions_cancel
                )

                build_pipeline = BuildPipeline()
                build_pipeline.provider = mock_github_provider
                build_pipeline.flat_manager = mock_flat_manager

                await build_pipeline.start_pipeline(new_pipeline_id)

    assert old_pipeline.status == PipelineStatus.SUPERSEDED
    mock_flat_manager.purge.assert_called_once_with(111)
    mock_github_actions_cancel.assert_called_once_with(
        str(old_pipeline_id), {"run_id": "12345"}
    )


@pytest.mark.asyncio
async def test_does_not_supersede_test_repo_pipelines():
    """Test that pipelines in 'test' repo are NOT superseded."""
    new_pipeline_id = uuid.uuid4()
    old_pipeline_id = uuid.uuid4()

    old_pipeline = Pipeline(
        id=old_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/123/head"},
        status=PipelineStatus.RUNNING,
        flat_manager_repo="test",
        build_id=111,
        provider_data={"run_id": "12345"},
        callback_token=str(uuid.uuid4()),
    )

    new_pipeline = Pipeline(
        id=new_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/456/head"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = new_pipeline

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = []
    mock_db_session.execute.return_value = mock_execute_result

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 99999, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    mock_flat_manager = MagicMock()
    mock_flat_manager.create_build = AsyncMock(return_value={"id": 99999})
    mock_flat_manager.create_token_subset = AsyncMock(return_value="upload-token")
    mock_flat_manager.get_build_url = MagicMock(
        return_value="https://flat-manager/build/99999"
    )
    mock_flat_manager.purge = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        with patch("httpx.AsyncClient") as mock_httpx_client:
            mock_get_db.return_value.__aenter__.return_value = mock_db_session
            mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

            build_pipeline = BuildPipeline()
            build_pipeline.provider = mock_github_provider
            build_pipeline.flat_manager = mock_flat_manager

            await build_pipeline.start_pipeline(new_pipeline_id)

    assert old_pipeline.status == PipelineStatus.RUNNING
    mock_flat_manager.purge.assert_not_called()


@pytest.mark.asyncio
async def test_supersedes_pending_pipeline_without_build_id():
    """Test that PENDING pipelines without build_id are superseded gracefully."""
    new_pipeline_id = uuid.uuid4()
    old_pipeline_id = uuid.uuid4()

    old_pipeline = Pipeline(
        id=old_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/heads/beta"},
        status=PipelineStatus.PENDING,
        flat_manager_repo="beta",
        build_id=None,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    new_pipeline = Pipeline(
        id=new_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/heads/beta"},
        status=PipelineStatus.PENDING,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = new_pipeline

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [old_pipeline]
    mock_db_session.execute.return_value = mock_execute_result

    mock_httpx_instance = MagicMock()
    mock_httpx_instance.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": 99999, "token": "test-token"}
    mock_httpx_instance.post.return_value = mock_response

    mock_github_provider = AsyncMock(spec=GitHubActionsService)
    mock_github_provider.dispatch = AsyncMock(return_value={"dispatch_result": "ok"})

    mock_flat_manager = MagicMock()
    mock_flat_manager.create_build = AsyncMock(return_value={"id": 99999})
    mock_flat_manager.create_token_subset = AsyncMock(return_value="upload-token")
    mock_flat_manager.get_build_url = MagicMock(
        return_value="https://flat-manager/build/99999"
    )
    mock_flat_manager.purge = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        with patch("httpx.AsyncClient") as mock_httpx_client:
            mock_get_db.return_value.__aenter__.return_value = mock_db_session
            mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

            build_pipeline = BuildPipeline()
            build_pipeline.provider = mock_github_provider
            build_pipeline.flat_manager = mock_flat_manager

            await build_pipeline.start_pipeline(new_pipeline_id)

    assert old_pipeline.status == PipelineStatus.SUPERSEDED
    mock_flat_manager.purge.assert_not_called()


@pytest.mark.asyncio
async def test_supersede_conflicting_test_pipelines_by_ref():
    new_pipeline_id = uuid.uuid4()
    old_pipeline_id = uuid.uuid4()

    old_pipeline = Pipeline(
        id=old_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/123/head"},
        status=PipelineStatus.RUNNING,
        flat_manager_repo="test",
        build_id=111,
        provider_data={"run_id": "12345"},
        callback_token=str(uuid.uuid4()),
    )

    new_pipeline = Pipeline(
        id=new_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/123/head", "build_type": "medium"},
        status=PipelineStatus.PENDING,
        flat_manager_repo="test",
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = new_pipeline

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [old_pipeline]
    mock_db_session.execute.return_value = mock_execute_result

    mock_flat_manager = MagicMock()
    mock_flat_manager.purge = AsyncMock()

    with (
        patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)),
        patch("app.pipelines.build.GitHubActionsService") as mock_actions_class,
    ):
        mock_actions_class.return_value.cancel = AsyncMock()
        build_pipeline = BuildPipeline()
        build_pipeline.flat_manager = mock_flat_manager

        await build_pipeline.supersede_conflicting_test_pipelines(new_pipeline_id)

    assert old_pipeline.status == PipelineStatus.SUPERSEDED
    mock_flat_manager.purge.assert_awaited_once_with(111)
    mock_actions_class.return_value.cancel.assert_awaited_once_with(
        str(old_pipeline_id), {"run_id": "12345"}
    )
    query = mock_db_session.execute.await_args.args[0]
    assert "refs/pull/123/head" in query.compile().params.values()


@pytest.mark.asyncio
async def test_supersede_conflicting_test_pipelines_does_not_supersede_different_ref():
    new_pipeline_id = uuid.uuid4()
    old_pipeline_id = uuid.uuid4()

    old_pipeline = Pipeline(
        id=old_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/456/head"},
        status=PipelineStatus.RUNNING,
        flat_manager_repo="test",
        build_id=111,
        provider_data={"run_id": "12345"},
        callback_token=str(uuid.uuid4()),
    )

    new_pipeline = Pipeline(
        id=new_pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/123/head", "build_type": "medium"},
        status=PipelineStatus.PENDING,
        flat_manager_repo="test",
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = new_pipeline

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = []
    mock_db_session.execute.return_value = mock_execute_result

    mock_flat_manager = MagicMock()
    mock_flat_manager.purge = AsyncMock()

    with (
        patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)),
        patch("app.pipelines.build.GitHubActionsService") as mock_actions_class,
    ):
        mock_actions_class.return_value.cancel = AsyncMock()
        build_pipeline = BuildPipeline()
        build_pipeline.flat_manager = mock_flat_manager

        await build_pipeline.supersede_conflicting_test_pipelines(new_pipeline_id)

    assert old_pipeline.status == PipelineStatus.RUNNING
    mock_flat_manager.purge.assert_not_awaited()
    mock_actions_class.return_value.cancel.assert_not_awaited()
    query = mock_db_session.execute.await_args.args[0]
    query_params = query.compile().params.values()
    assert "refs/pull/123/head" in query_params
    assert "refs/pull/456/head" not in query_params


@pytest.mark.asyncio
async def test_start_pending_builds_respects_capacity():
    pending_pipeline_id_1 = uuid.uuid4()

    count_result = MagicMock()
    count_result.scalar.return_value = 1

    pending_result = MagicMock()
    pending_result.fetchall.return_value = [
        (pending_pipeline_id_1,),
    ]

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.execute = AsyncMock(side_effect=[count_result, pending_result])

    build_pipeline = BuildPipeline()

    async def _start_pipeline(pipeline_id):
        started = MagicMock(spec=Pipeline)
        started.id = pipeline_id
        return started

    build_pipeline.start_pipeline = AsyncMock(side_effect=_start_pipeline)  # ty: ignore[invalid-assignment]

    with (
        patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)),
        patch("app.pipelines.build.settings.max_concurrent_builds", 2),
    ):
        started_ids = await build_pipeline.start_pending_builds()

    assert started_ids == [pending_pipeline_id_1]
    build_pipeline.start_pipeline.assert_awaited_once_with(pending_pipeline_id_1)  # ty: ignore[unresolved-attribute]
    pending_query_params = mock_db_session.execute.await_args_list[1].args[1]
    assert pending_query_params["limit"] == 1


@pytest.mark.asyncio
async def test_start_pending_builds_unlimited():
    pending_pipeline_id_1 = uuid.uuid4()
    pending_pipeline_id_2 = uuid.uuid4()

    pending_result = MagicMock()
    pending_result.fetchall.return_value = [
        (pending_pipeline_id_1,),
        (pending_pipeline_id_2,),
    ]

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.execute = AsyncMock(return_value=pending_result)

    build_pipeline = BuildPipeline()

    async def _start_pipeline(pipeline_id):
        started = MagicMock(spec=Pipeline)
        started.id = pipeline_id
        return started

    build_pipeline.start_pipeline = AsyncMock(side_effect=_start_pipeline)  # ty: ignore[invalid-assignment]

    with (
        patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)),
        patch("app.pipelines.build.settings.max_concurrent_builds", 0),
    ):
        started_ids = await build_pipeline.start_pending_builds()

    assert started_ids == [pending_pipeline_id_1, pending_pipeline_id_2]
    assert build_pipeline.start_pipeline.await_count == 2  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flat_manager_repo, should_supersede",
    [
        ("stable", False),
        ("test", True),
    ],
)
async def test_supersede_conflicting_test_pipelines(
    flat_manager_repo, should_supersede
):
    pipeline_id = uuid.uuid4()
    pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/1/head", "build_type": "medium"},
        status=PipelineStatus.PENDING,
        flat_manager_repo=flat_manager_repo,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = pipeline

    build_pipeline = BuildPipeline()
    build_pipeline._supersede_conflicting_pipelines = AsyncMock()  # ty: ignore[invalid-assignment]

    with patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)):
        await build_pipeline.supersede_conflicting_test_pipelines(pipeline_id)

    if should_supersede:
        build_pipeline._supersede_conflicting_pipelines.assert_awaited_once_with(  # ty: ignore[unresolved-attribute]
            db=mock_db_session,
            pipeline=pipeline,
            flat_manager_repo="test",
        )
    else:
        build_pipeline._supersede_conflicting_pipelines.assert_not_awaited()  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flat_manager_repo, build_type, can_start, expected",
    [
        ("stable", "medium", True, False),
        ("test", "default", True, False),
        ("test", "medium", True, False),
        ("test", "medium", False, True),
        ("test", "large", False, True),
        ("test", "large", True, False),
    ],
)
async def test_should_queue_test_build(
    flat_manager_repo, build_type, can_start, expected
):
    pipeline_id = uuid.uuid4()
    pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.example.app",
        params={"ref": "refs/pull/1/head", "build_type": build_type},
        status=PipelineStatus.PENDING,
        flat_manager_repo=flat_manager_repo,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = pipeline

    build_pipeline = BuildPipeline()
    build_pipeline._can_start_test_spot_build = AsyncMock(return_value=can_start)  # ty: ignore[invalid-assignment]

    with patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)):
        result = await build_pipeline.should_queue_test_build(pipeline_id)

    assert result is expected

    if flat_manager_repo == "test" and build_type in ("medium", "large"):
        build_pipeline._can_start_test_spot_build.assert_awaited_once_with(  # ty: ignore[unresolved-attribute]
            mock_db_session
        )
    else:
        build_pipeline._can_start_test_spot_build.assert_not_awaited()  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_start_pending_builds_at_zero_capacity():
    count_result = MagicMock()
    count_result.scalar.return_value = 20

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.execute = AsyncMock(return_value=count_result)

    build_pipeline = BuildPipeline()
    build_pipeline.start_pipeline = AsyncMock()  # ty: ignore[invalid-assignment]

    with (
        patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)),
        patch("app.pipelines.build.settings.max_concurrent_builds", 20),
    ):
        started_ids = await build_pipeline.start_pending_builds()

    assert started_ids == []
    build_pipeline.start_pipeline.assert_not_awaited()  # ty: ignore[unresolved-attribute]
    assert mock_db_session.execute.await_count == 1


@pytest.mark.asyncio
async def test_start_pending_builds_handles_non_value_error():
    pending_id_1 = uuid.uuid4()
    pending_id_2 = uuid.uuid4()

    pending_result = MagicMock()
    pending_result.fetchall.return_value = [
        (pending_id_1,),
        (pending_id_2,),
    ]

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.execute = AsyncMock(return_value=pending_result)

    started_pipeline_2 = MagicMock(spec=Pipeline)
    started_pipeline_2.id = pending_id_2

    build_pipeline = BuildPipeline()
    build_pipeline.start_pipeline = AsyncMock(  # ty: ignore[invalid-assignment]
        side_effect=[RuntimeError("flat-manager down"), started_pipeline_2]
    )

    with (
        patch("app.pipelines.build.get_db", create_mock_get_db(mock_db_session)),
        patch("app.pipelines.build.settings.max_concurrent_builds", 0),
    ):
        started_ids = await build_pipeline.start_pending_builds()

    assert started_ids == [pending_id_2]
    assert build_pipeline.start_pipeline.await_count == 2  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_supersede_skips_when_test_pipeline_has_no_ref():
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.example.app",
        params={},
        status=PipelineStatus.PENDING,
        flat_manager_repo="test",
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )

    mock_db_session = AsyncMock(spec=AsyncSession)

    build_pipeline = BuildPipeline()
    await build_pipeline._supersede_conflicting_pipelines(
        db=mock_db_session,
        pipeline=pipeline,
        flat_manager_repo="test",
    )

    mock_db_session.execute.assert_not_awaited()
