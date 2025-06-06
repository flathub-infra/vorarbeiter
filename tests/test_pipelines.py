import uuid
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.services import GitHubActionsService
from app.pipelines.build import BuildPipeline, CallbackData


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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db

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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db

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
async def test_handle_callback_success(build_pipeline, mock_db, sample_pipeline):
    mock_db.get.return_value = sample_pipeline

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db

    callback_data = CallbackData(status="success")

    with patch("app.pipelines.build.get_db", mock_get_db):
        pipeline, updates = await build_pipeline.handle_callback(
            sample_pipeline.id, callback_data
        )

    assert pipeline.status == PipelineStatus.SUCCEEDED
    assert pipeline.finished_at is not None


@pytest.mark.asyncio
async def test_handle_callback_failure(build_pipeline, mock_db, sample_pipeline):
    mock_db.get.return_value = sample_pipeline

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db

    callback_data = CallbackData(status="failure")

    with patch("app.pipelines.build.get_db", mock_get_db):
        pipeline, updates = await build_pipeline.handle_callback(
            sample_pipeline.id, callback_data
        )

    assert pipeline.status == PipelineStatus.FAILED
    assert pipeline.finished_at is not None


@pytest.fixture
def mock_db_session():
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    return mock_session


@pytest.fixture
def mock_get_db(mock_db_session):
    @asynccontextmanager
    async def _mock_get_db():
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
    assert response.status_code == 403  # Missing Authorization header

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
            build_id="build-123",
            created_at=datetime.now(),
            started_at=datetime.now(),
            finished_at=None,
            published_at=None,
        ),
        MagicMock(
            id=uuid.uuid4(),
            app_id="org.flathub.Test2",
            status=PipelineStatus.SUCCEEDED,
            flat_manager_repo="beta",
            triggered_by=PipelineTrigger.WEBHOOK,
            build_id="build-456",
            created_at=datetime.now(),
            started_at=datetime.now(),
            finished_at=datetime.now(),
            published_at=None,
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


def test_pipeline_callback_status_endpoint(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"status": "success", "result": {"output": "Build successful"}}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["pipeline_status"] == "success"


def test_pipeline_callback_log_url_endpoint(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"log_url": "https://example.com/logs/12345"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["log_url"] == "https://example.com/logs/12345"
    assert sample_pipeline.log_url == "https://example.com/logs/12345"


def test_pipeline_callback_invalid_data(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"some_key": "some_value"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 400
    assert (
        "Request must contain either 'status', 'log_url', 'app_id', 'is_extra_data', 'end_of_life', or 'end_of_life_rebase' field"
        in response.json()["detail"]
    )


def test_pipeline_callback_invalid_status(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"status": "invalid_status"}
        headers = {"Authorization": "Bearer test_token_12345"}

        try:
            test_client.post(
                f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
            )
            assert False
        except Exception:
            pass


def test_pipeline_callback_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = None

        data = {"status": "success", "result": {"output": "Build successful"}}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]


def test_pipeline_callback_invalid_token(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"status": "success", "result": {"output": "Build successful"}}
        headers = {"Authorization": "Bearer wrong_token"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 401
    assert "Invalid callback token" in response.json()["detail"]


def test_pipeline_callback_status_immutable(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    sample_pipeline.status = PipelineStatus.SUCCEEDED

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"status": "success", "result": {"output": "Build successful"}}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 409
    assert "Pipeline status already finalized" in response.json()["detail"]


def test_pipeline_callback_log_url_immutable(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    sample_pipeline.log_url = "https://example.com/logs/existing"

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"log_url": "https://example.com/logs/new"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 409
    assert "Log URL already set" in response.json()["detail"]


def test_redirect_to_log_url(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    sample_pipeline.log_url = "https://example.com/logs/12345"

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

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

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with patch("app.routes.pipelines.get_db", mock_get_db_session):
        mock_get_db.get.return_value = sample_pipeline

        response = test_client.get(f"/api/pipelines/{pipeline_id}/log_url")

    assert response.status_code == 202
    assert "Retry-After" in response.headers
    assert "Log URL not available yet" in response.json()["detail"]


def test_redirect_to_log_url_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with patch("app.routes.pipelines.get_db", mock_get_db_session):
        mock_get_db.get.return_value = None

        response = test_client.get(f"/api/pipelines/{pipeline_id}/log_url")

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]


def test_pipeline_callback_end_of_life_only(mock_get_db, sample_pipeline):
    """Test setting only the end_of_life field via callback."""
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"end_of_life": "This app is deprecated"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["end_of_life"] == "This app is deprecated"
    assert sample_pipeline.end_of_life == "This app is deprecated"
    assert sample_pipeline.end_of_life_rebase is None
    # Ensure status was not changed
    assert sample_pipeline.status == PipelineStatus.PENDING


def test_pipeline_callback_end_of_life_rebase_only(mock_get_db, sample_pipeline):
    """Test setting only the end_of_life_rebase field via callback."""
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {"end_of_life_rebase": "org.flathub.NewApp"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["end_of_life_rebase"] == "org.flathub.NewApp"
    assert sample_pipeline.end_of_life_rebase == "org.flathub.NewApp"
    assert sample_pipeline.end_of_life is None
    # Ensure status was not changed
    assert sample_pipeline.status == PipelineStatus.PENDING


def test_pipeline_callback_both_end_of_life_fields(mock_get_db, sample_pipeline):
    """Test setting both end_of_life fields via callback."""
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
    ):
        mock_get_db.get.return_value = sample_pipeline

        data = {
            "end_of_life": "This app is deprecated",
            "end_of_life_rebase": "org.flathub.NewApp",
        }
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["end_of_life"] == "This app is deprecated"
    assert response.json()["end_of_life_rebase"] == "org.flathub.NewApp"
    assert sample_pipeline.end_of_life == "This app is deprecated"
    assert sample_pipeline.end_of_life_rebase == "org.flathub.NewApp"
    # Ensure status was not changed
    assert sample_pipeline.status == PipelineStatus.PENDING


def test_pipeline_callback_end_of_life_with_status(mock_get_db, sample_pipeline):
    """Test setting end_of_life fields along with status update."""
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    # Start with a running pipeline
    sample_pipeline.status = PipelineStatus.RUNNING
    sample_pipeline.build_id = "build-123"
    sample_pipeline.params = {"sha": "abc123", "repo": "flathub/test-app"}

    mock_flat_manager = MagicMock()
    mock_flat_manager.commit = AsyncMock()

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
        patch("app.pipelines.build.FlatManagerClient") as mock_fm_class,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier_class,
    ):
        mock_fm_class.return_value = mock_flat_manager
        mock_get_db.get.return_value = sample_pipeline
        mock_github_notifier = MagicMock()
        mock_github_notifier.handle_build_completion = AsyncMock()
        mock_github_notifier_class.return_value = mock_github_notifier

        data = {
            "status": "success",
            "end_of_life": "This app is deprecated",
            "end_of_life_rebase": "org.flathub.NewApp",
        }
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["pipeline_status"] == "success"
    # The end_of_life fields are now returned in the response for status updates
    assert response.json()["end_of_life"] == "This app is deprecated"
    assert response.json()["end_of_life_rebase"] == "org.flathub.NewApp"

    # But they should be set on the pipeline object
    assert sample_pipeline.end_of_life == "This app is deprecated"
    assert sample_pipeline.end_of_life_rebase == "org.flathub.NewApp"
    assert sample_pipeline.status == PipelineStatus.SUCCEEDED
    assert sample_pipeline.finished_at is not None

    # Verify that flat_manager.commit was called with the end_of_life parameters
    mock_flat_manager.commit.assert_called_once_with(
        "build-123",
        end_of_life="This app is deprecated",
        end_of_life_rebase="org.flathub.NewApp",
    )


def test_pipeline_callback_status_update_preserves_existing_end_of_life(
    mock_get_db, sample_pipeline
):
    """Test that status update preserves existing end_of_life fields."""
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    # Start with a running pipeline that already has end_of_life fields set
    sample_pipeline.status = PipelineStatus.RUNNING
    sample_pipeline.build_id = "build-456"
    sample_pipeline.end_of_life = "Already deprecated"
    sample_pipeline.end_of_life_rebase = "org.flathub.ExistingApp"
    sample_pipeline.params = {"sha": "def456", "repo": "flathub/test-app"}

    mock_flat_manager = MagicMock()
    mock_flat_manager.commit = AsyncMock()

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
        patch("app.pipelines.build.FlatManagerClient") as mock_fm_class,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier_class,
    ):
        mock_fm_class.return_value = mock_flat_manager
        mock_get_db.get.return_value = sample_pipeline
        mock_github_notifier = MagicMock()
        mock_github_notifier.handle_build_completion = AsyncMock()
        mock_github_notifier_class.return_value = mock_github_notifier

        # Only send status update, no end_of_life fields
        data = {"status": "success"}
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["pipeline_status"] == "success"

    # Ensure existing end_of_life fields are preserved
    assert sample_pipeline.end_of_life == "Already deprecated"
    assert sample_pipeline.end_of_life_rebase == "org.flathub.ExistingApp"
    assert sample_pipeline.status == PipelineStatus.SUCCEEDED

    # Verify that flat_manager.commit was called with the existing end_of_life parameters
    mock_flat_manager.commit.assert_called_once_with(
        "build-456",
        end_of_life="Already deprecated",
        end_of_life_rebase="org.flathub.ExistingApp",
    )


def test_pipeline_callback_early_exit_bug_regression(mock_get_db, sample_pipeline):
    """
    Regression test for the early exit bug where setting end_of_life fields
    would cause the callback to return early without processing the status update.
    This ensures the bug fix is working correctly.
    """
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    # Start with a running pipeline
    sample_pipeline.status = PipelineStatus.RUNNING
    sample_pipeline.build_id = "build-789"
    sample_pipeline.params = {"sha": "xyz789", "repo": "flathub/test-app"}

    mock_flat_manager = MagicMock()
    mock_flat_manager.commit = AsyncMock()

    @asynccontextmanager
    async def mock_get_db_session():
        yield mock_get_db

    with (
        patch("app.routes.pipelines.get_db", mock_get_db_session),
        patch("app.pipelines.build.get_db", mock_get_db_session),
        patch("app.pipelines.build.FlatManagerClient") as mock_fm_class,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier_class,
    ):
        mock_fm_class.return_value = mock_flat_manager
        mock_get_db.get.return_value = sample_pipeline
        mock_github_notifier = MagicMock()
        mock_github_notifier.handle_build_completion = AsyncMock()
        mock_github_notifier_class.return_value = mock_github_notifier

        # Send both status AND end_of_life fields - this used to cause early exit
        data = {
            "status": "success",
            "end_of_life": "App renamed to: org.luanti.luanti. Read: https://blog.luanti.org/2024/10/13/Introducing-Our-New-Name/",
            "end_of_life_rebase": "org.luanti.luanti",
        }
        headers = {"Authorization": "Bearer test_token_12345"}

        response = test_client.post(
            f"/api/pipelines/{pipeline_id}/callback", json=data, headers=headers
        )

    # The response should indicate successful status update
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["pipeline_id"] == str(pipeline_id)
    assert response.json()["pipeline_status"] == "success"

    # CRITICAL: Verify that the pipeline status was actually updated
    # This is what was broken before - the status remained "running"
    assert sample_pipeline.status == PipelineStatus.SUCCEEDED
    assert sample_pipeline.finished_at is not None

    # Verify end_of_life fields were also set
    assert (
        sample_pipeline.end_of_life
        == "App renamed to: org.luanti.luanti. Read: https://blog.luanti.org/2024/10/13/Introducing-Our-New-Name/"
    )
    assert sample_pipeline.end_of_life_rebase == "org.luanti.luanti"

    # Verify that flat_manager.commit was called with all parameters
    mock_flat_manager.commit.assert_called_once_with(
        "build-789",
        end_of_life="App renamed to: org.luanti.luanti. Read: https://blog.luanti.org/2024/10/13/Introducing-Our-New-Name/",
        end_of_life_rebase="org.luanti.luanti",
    )

    # Verify that GitHub notifier was called
    mock_github_notifier.handle_build_completion.assert_called_once()
