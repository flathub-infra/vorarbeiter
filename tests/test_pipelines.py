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
def mock_db():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def mock_provider():
    provider = AsyncMock(spec=GitHubActionsService)
    return provider


@pytest.fixture
def build_pipeline(mock_provider):
    with patch("app.services.github_actions_service", mock_provider):
        pipeline = BuildPipeline()
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
            result = await build_pipeline.start_pipeline(pipeline_id)

    assert result.status == PipelineStatus.RUNNING
    assert build_pipeline.provider.dispatch.called
    assert mock_httpx_client.__aenter__.return_value.post.call_count == 2

    dispatch_call_args = build_pipeline.provider.dispatch.call_args[0]
    job_data = dispatch_call_args[2]
    assert job_data["params"]["inputs"]["flat_manager_token"] == "test-token"


@pytest.mark.asyncio
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
def mock_db_session():
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    return mock_session


@pytest.fixture
def mock_get_db(mock_db_session):
    @asynccontextmanager
    async def _mock_get_db(*, use_replica: bool = False):
        yield mock_db_session

    with patch("app.routes.pipelines.get_db", _mock_get_db):
        yield mock_db_session


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
            mock_get_db.return_value.__aenter__.return_value = mock_db_session
            mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

            build_pipeline = BuildPipeline()
            build_pipeline.provider = mock_github_provider

            await build_pipeline.start_pipeline(pipeline_id)

            assert mock_pipeline.params["build_type"] == "medium"


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
            mock_get_db.return_value.__aenter__.return_value = mock_db_session
            mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

            build_pipeline = BuildPipeline()
            build_pipeline.provider = mock_github_provider

            await build_pipeline.start_pipeline(pipeline_id)

            assert mock_pipeline.params["build_type"] == "medium"


@pytest.mark.asyncio
async def test_start_pipeline_build_type_precedence():
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.chromium.Chromium",
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
            mock_get_db.return_value.__aenter__.return_value = mock_db_session
            mock_httpx_client.return_value.__aenter__.return_value = mock_httpx_instance

            build_pipeline = BuildPipeline()
            build_pipeline.provider = mock_github_provider

            await build_pipeline.start_pipeline(pipeline_id)

            assert mock_pipeline.params["build_type"] == "large"


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
