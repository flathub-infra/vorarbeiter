import uuid
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.providers import JobProvider, ProviderType
from app.pipelines.build import BuildPipeline


@pytest.fixture
def mock_db():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def mock_provider():
    provider = AsyncMock(spec=JobProvider)
    provider.provider_type = ProviderType.GITHUB
    return provider


@pytest.fixture
def build_pipeline(mock_provider):
    with patch("app.providers.get_provider", return_value=mock_provider):
        pipeline = BuildPipeline()
        return pipeline


@pytest.fixture
def sample_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.flathub.Test",
        status=PipelineStatus.PENDING,
        params={"repo": "test", "branch": "main"},
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider=ProviderType.GITHUB.value,
        provider_data={},
        callback_token="test_token_12345",
    )


@pytest.mark.asyncio
async def test_create_pipeline(build_pipeline, mock_db, monkeypatch):
    app_id = "org.flathub.Test"
    params = {"repo": "test", "branch": "main"}

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
    build_pipeline.github_provider.dispatch = AsyncMock(return_value=dispatch_result)

    with patch("app.pipelines.build.get_db", mock_get_db):
        result = await build_pipeline.start_pipeline(pipeline_id)

    assert result.status == PipelineStatus.RUNNING
    assert build_pipeline.github_provider.dispatch.called


@pytest.mark.asyncio
@patch("app.pipelines.build.get_db")
@patch("app.pipelines.build.get_provider")
@pytest.mark.parametrize(
    "source_branch, expected_target_branch",
    [
        ("master", "stable"),
        ("beta", "beta"),
        ("feature/new-thing", "test"),
        (None, "test"),
        ("branch/my-feature", "my-feature"),
    ],
)
async def test_start_pipeline_branch_mapping(
    mock_get_provider,
    mock_get_db,
    source_branch,
    expected_target_branch,
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
        provider=ProviderType.GITHUB.value,
        provider_data={},
        callback_token=str(uuid.uuid4()),
    )
    mock_db_session.get.return_value = mock_pipeline
    mock_get_db.return_value.__aenter__.return_value = mock_db_session

    mock_github_provider = AsyncMock()
    mock_get_provider.return_value = mock_github_provider
    mock_github_provider.dispatch.return_value = {"dispatch_result": "ok"}

    build_pipeline = BuildPipeline()
    build_pipeline.github_provider = mock_github_provider

    await build_pipeline.start_pipeline(pipeline_id)

    mock_db_session.get.assert_called_once_with(Pipeline, pipeline_id)
    assert mock_pipeline.status == PipelineStatus.RUNNING
    assert mock_pipeline.started_at is not None

    mock_github_provider.dispatch.assert_called_once()
    call_args, call_kwargs = mock_github_provider.dispatch.call_args
    dispatched_job_data = call_args[2]
    assert dispatched_job_data["params"]["inputs"]["branch"] == expected_target_branch
    assert mock_pipeline.provider_data == {"dispatch_result": "ok"}

    mock_db_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_handle_callback_success(build_pipeline, mock_db, sample_pipeline):
    status = "success"
    result = {"output": "Build successful"}

    await build_pipeline.update_pipeline_status(
        pipeline=sample_pipeline, status=status, result=result
    )

    assert sample_pipeline.status == PipelineStatus.SUCCEEDED
    assert sample_pipeline.finished_at is not None
    assert sample_pipeline.result == result


@pytest.mark.asyncio
async def test_handle_callback_failure(build_pipeline, mock_db, sample_pipeline):
    status = "failure"
    result = {"error": "Build failed"}

    await build_pipeline.update_pipeline_status(
        pipeline=sample_pipeline, status=status, result=result
    )

    assert sample_pipeline.status == PipelineStatus.FAILED
    assert sample_pipeline.finished_at is not None
    assert sample_pipeline.result == result


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
def mock_build_pipeline():
    with patch("app.routes.pipelines.BuildPipeline") as pipeline_class_mock:
        pipeline_mock = AsyncMock()

        mock_pipeline = MagicMock(spec=Pipeline)
        mock_pipeline.id = uuid.uuid4()
        mock_pipeline.app_id = "org.flathub.Test"

        mock_status = MagicMock()
        type(mock_status).value = PropertyMock(return_value="running")
        mock_pipeline.status = mock_status

        pipeline_mock.create_pipeline = AsyncMock(return_value=mock_pipeline)
        pipeline_mock.start_pipeline = AsyncMock(return_value=mock_pipeline)

        pipeline_class_mock.return_value = pipeline_mock
        yield pipeline_class_mock


def test_trigger_pipeline_endpoint(mock_get_db, mock_build_pipeline):
    from app.config import settings

    test_client = TestClient(app)

    request_data = {
        "app_id": "org.flathub.Test",
        "params": {"repo": "test", "branch": "main"},
    }

    headers = {"Authorization": f"Bearer {settings.admin_token}"}

    response = test_client.post("/api/pipelines", json=request_data, headers=headers)

    assert response.status_code == 201
    assert "pipeline_id" in response.json()
    assert response.json()["app_id"] == "org.flathub.Test"
    assert response.json()["status"] == "created"
    assert response.json()["pipeline_status"] == "running"

    pipeline_instance = mock_build_pipeline.return_value

    pipeline_instance.create_pipeline.assert_called_once()
    call_kwargs = pipeline_instance.create_pipeline.call_args[1]
    assert call_kwargs["app_id"] == "org.flathub.Test"
    assert call_kwargs["params"] == {"repo": "test", "branch": "main"}
    assert call_kwargs["webhook_event_id"] is None

    pipeline_instance.start_pipeline.assert_called_once()

    assert mock_get_db.flush.called


def test_trigger_pipeline_unauthorized(mock_get_db, mock_build_pipeline):
    test_client = TestClient(app)

    request_data = {
        "app_id": "org.flathub.Test",
        "params": {"repo": "test", "branch": "main"},
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
            triggered_by=PipelineTrigger.MANUAL,
            created_at=datetime.now(),
            started_at=datetime.now(),
            finished_at=None,
            published_at=None,
        ),
        MagicMock(
            id=uuid.uuid4(),
            app_id="org.flathub.Test2",
            status=PipelineStatus.SUCCEEDED,
            triggered_by=PipelineTrigger.WEBHOOK,
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
    assert response.json()[1]["app_id"] == "org.flathub.Test2"
    assert response.json()[1]["status"] == "succeeded"
    assert response.json()[1]["triggered_by"] == "webhook"


def test_get_pipeline_endpoint(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db.get.return_value = sample_pipeline

    response = test_client.get(f"/api/pipelines/{pipeline_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(pipeline_id)
    assert response.json()["app_id"] == sample_pipeline.app_id
    assert response.json()["status"] == sample_pipeline.status.value
    assert response.json()["provider"] == sample_pipeline.provider


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
        "Request must contain either 'status' or 'log_url' field"
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
