import uuid
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import Pipeline, PipelineStatus, Job, JobStatus, PipelineTrigger
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
    return BuildPipeline(mock_provider)


@pytest.fixture
def sample_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.flathub.Test",
        status=PipelineStatus.PENDING,
        params={"repo": "test", "branch": "main"},
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
    )


@pytest.fixture
def validate_job(sample_pipeline):
    return Job(
        id=uuid.uuid4(),
        pipeline_id=sample_pipeline.id,
        job_type="validate_manifest",
        status=JobStatus.PENDING,
        provider=ProviderType.GITHUB.value,
        provider_data={},
        position=0,
        created_at=datetime.now(),
    )


@pytest.fixture
def build_job(sample_pipeline):
    return Job(
        id=uuid.uuid4(),
        pipeline_id=sample_pipeline.id,
        job_type="build",
        status=JobStatus.PENDING,
        provider=ProviderType.GITHUB.value,
        provider_data={},
        position=1,
        created_at=datetime.now(),
    )


@pytest.mark.asyncio
async def test_create_pipeline(build_pipeline, mock_db, monkeypatch):
    app_id = "org.flathub.Test"
    params = {"repo": "test", "branch": "main"}

    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()

    test_pipeline = MagicMock(spec=Pipeline)
    test_pipeline.id = uuid.uuid4()
    test_pipeline.app_id = app_id
    test_pipeline.params = params
    test_pipeline.status = PipelineStatus.PENDING

    with patch("app.pipelines.build.Pipeline", return_value=test_pipeline):
        with patch("app.pipelines.build.Job"):
            result = await build_pipeline.create_pipeline(mock_db, app_id, params)

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

    validate_job = MagicMock(spec=Job)
    validate_job.id = uuid.uuid4()
    validate_job.pipeline_id = pipeline_id
    validate_job.job_type = "validate_manifest"
    validate_job.status = JobStatus.PENDING

    async def mock_get(model_class, model_id):
        if model_class is Pipeline and model_id == pipeline_id:
            return mock_pipeline
        return None

    mock_db.get = AsyncMock(side_effect=mock_get)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = validate_job
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_db.commit = AsyncMock()

    dispatch_result = {"status": "dispatched"}
    build_pipeline.github_provider.dispatch = AsyncMock(return_value=dispatch_result)

    result = await build_pipeline.start_pipeline(mock_db, pipeline_id)

    assert result.status == PipelineStatus.RUNNING
    assert mock_db.commit.called
    assert build_pipeline.github_provider.dispatch.called


@pytest.mark.asyncio
async def test_prepare_build(
    build_pipeline, mock_db, sample_pipeline, validate_job, monkeypatch
):
    mock_db.get.side_effect = [validate_job, sample_pipeline]
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()

    validate_job.result = {"manifest_path": "/path/to/manifest.yml"}

    def mock_add(obj):
        if isinstance(obj, Job):
            monkeypatch.setattr(obj, "id", uuid.uuid4())

    mock_db.add.side_effect = mock_add

    result = await build_pipeline.prepare_build(mock_db, validate_job.id)

    assert result.job_type == "build"
    assert result.pipeline_id == sample_pipeline.id
    assert result.status == JobStatus.PENDING
    assert result.provider == ProviderType.GITHUB.value
    assert result.position == 1
    assert "manifest_path" in result.provider_data.get("additional_params", {})
    assert mock_db.commit.called


@pytest.mark.asyncio
async def test_start_build(build_pipeline, mock_db, sample_pipeline, build_job):
    mock_db.get.side_effect = [build_job, sample_pipeline]
    mock_db.commit = AsyncMock()

    build_pipeline.github_provider.dispatch = AsyncMock(
        return_value={"status": "dispatched"}
    )

    build_job.provider_data = {
        "additional_params": {"manifest_path": "/path/to/manifest.yml"}
    }

    result = await build_pipeline.start_build(mock_db, build_job.id)

    assert result.status == JobStatus.RUNNING
    assert result.started_at is not None
    assert "status" in result.provider_data
    assert "additional_params" in result.provider_data
    assert mock_db.commit.called
    assert build_pipeline.github_provider.dispatch.called


@pytest.mark.asyncio
async def test_publish(build_pipeline, mock_db, sample_pipeline, build_job):
    mock_db.get.side_effect = [build_job, sample_pipeline]
    mock_db.commit = AsyncMock()

    result = await build_pipeline.publish(mock_db, build_job.id)

    assert result.status == PipelineStatus.PUBLISHED
    assert result.published_at is not None
    assert result.finished_at is not None
    assert mock_db.commit.called


@pytest.mark.asyncio
async def test_handle_job_callback_success(
    build_pipeline, mock_db, sample_pipeline, validate_job
):
    mock_db.get.side_effect = [validate_job, sample_pipeline]
    mock_db.commit = AsyncMock()

    status = "success"
    result = {"output": "Validation successful"}

    job = await build_pipeline.handle_job_callback(
        mock_db, validate_job.id, status, result
    )

    assert job.status == JobStatus.COMPLETE
    assert job.finished_at is not None
    assert job.result == result
    assert mock_db.commit.called


@pytest.mark.asyncio
async def test_handle_job_callback_failure(
    build_pipeline, mock_db, sample_pipeline, validate_job
):
    mock_db.get.side_effect = [validate_job, sample_pipeline]
    mock_db.commit = AsyncMock()

    status = "failure"
    result = {"error": "Validation failed"}

    job = await build_pipeline.handle_job_callback(
        mock_db, validate_job.id, status, result
    )

    assert job.status == JobStatus.FAILED
    assert job.finished_at is not None
    assert job.result == result
    assert sample_pipeline.status == PipelineStatus.FAILED
    assert sample_pipeline.finished_at is not None
    assert mock_db.commit.called


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
def mock_provider_factory():
    with patch("app.routes.pipelines.ProviderFactory") as factory_mock:
        provider_mock = AsyncMock(spec=JobProvider)
        factory_mock.create_provider = AsyncMock(return_value=provider_mock)
        yield factory_mock


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


def test_trigger_pipeline_endpoint(
    mock_get_db, mock_provider_factory, mock_build_pipeline
):
    from app.config import settings

    test_client = TestClient(app)

    request_data = {
        "app_id": "org.flathub.Test",
        "params": {"repo": "test", "branch": "main"},
    }

    headers = {"X-API-Token": settings.api_token}

    response = test_client.post("/api/pipelines", json=request_data, headers=headers)

    assert response.status_code == 201
    assert "pipeline_id" in response.json()
    assert response.json()["app_id"] == "org.flathub.Test"
    assert response.json()["status"] == "created"
    assert response.json()["pipeline_status"] == "running"

    mock_provider_factory.create_provider.assert_called_once()

    pipeline_instance = mock_build_pipeline.return_value

    pipeline_instance.create_pipeline.assert_called_once()
    call_kwargs = pipeline_instance.create_pipeline.call_args[1]
    assert call_kwargs["app_id"] == "org.flathub.Test"
    assert call_kwargs["params"] == {"repo": "test", "branch": "main"}
    assert call_kwargs["webhook_event_id"] is None

    pipeline_instance.start_pipeline.assert_called_once()

    assert mock_get_db.flush.called


def test_trigger_pipeline_unauthorized(
    mock_get_db, mock_provider_factory, mock_build_pipeline
):
    test_client = TestClient(app)

    request_data = {
        "app_id": "org.flathub.Test",
        "params": {"repo": "test", "branch": "main"},
    }

    # Test with no token
    response = test_client.post("/api/pipelines", json=request_data)
    assert response.status_code == 422  # Missing required header

    # Test with invalid token
    headers = {"X-API-Token": "invalid-token"}
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
            status=PipelineStatus.COMPLETE,
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
    assert response.json()[1]["status"] == "complete"
    assert response.json()[1]["triggered_by"] == "webhook"


@pytest.fixture
def mock_jobs():
    return [
        MagicMock(
            id=uuid.uuid4(),
            job_type="validate_manifest",
            status=JobStatus.RUNNING,
            position=0,
            provider="github",
            created_at=datetime.now(),
            started_at=datetime.now(),
            finished_at=None,
            result=None,
        ),
        MagicMock(
            id=uuid.uuid4(),
            job_type="build",
            status=JobStatus.PENDING,
            position=1,
            provider="github",
            created_at=datetime.now(),
            started_at=None,
            finished_at=None,
            result=None,
        ),
    ]


def test_get_pipeline_endpoint(mock_get_db, sample_pipeline, mock_jobs):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db.get.return_value = sample_pipeline

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_jobs
    mock_get_db.execute.return_value = mock_result

    response = test_client.get(f"/api/pipelines/{pipeline_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(pipeline_id)
    assert response.json()["app_id"] == sample_pipeline.app_id
    assert response.json()["status"] == sample_pipeline.status.value
    assert len(response.json()["jobs"]) == 2

    assert response.json()["jobs"][0]["job_type"] == "validate_manifest"
    assert response.json()["jobs"][0]["position"] == 0
    assert response.json()["jobs"][1]["job_type"] == "build"
    assert response.json()["jobs"][1]["position"] == 1


def test_get_pipeline_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()

    mock_get_db.get.return_value = None

    response = test_client.get(f"/api/pipelines/{pipeline_id}")

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]


def test_get_job_endpoint(mock_get_db, sample_pipeline, mock_jobs):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    job = mock_jobs[0]
    job_id = job.id

    # Set the pipeline_id on the mock job to match the sample pipeline
    job.pipeline_id = pipeline_id

    mock_get_db.get.side_effect = [sample_pipeline, job]

    response = test_client.get(f"/api/pipelines/{pipeline_id}/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(job_id)
    assert response.json()["job_type"] == job.job_type
    assert response.json()["status"] == job.status.value
    assert response.json()["position"] == job.position


def test_get_job_pipeline_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()
    job_id = uuid.uuid4()

    mock_get_db.get.return_value = None

    response = test_client.get(f"/api/pipelines/{pipeline_id}/jobs/{job_id}")

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]


def test_get_job_not_found(mock_get_db, sample_pipeline):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    job_id = uuid.uuid4()

    mock_get_db.get.side_effect = [sample_pipeline, None]

    response = test_client.get(f"/api/pipelines/{pipeline_id}/jobs/{job_id}")

    assert response.status_code == 404
    assert f"Job {job_id} not found" in response.json()["detail"]


def test_get_job_wrong_pipeline(mock_get_db, sample_pipeline, mock_jobs):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id
    job = mock_jobs[0]

    wrong_pipeline_id = uuid.uuid4()
    job.pipeline_id = wrong_pipeline_id

    mock_get_db.get.side_effect = [sample_pipeline, job]

    response = test_client.get(f"/api/pipelines/{pipeline_id}/jobs/{job.id}")

    assert response.status_code == 400
    assert (
        f"Job {job.id} does not belong to pipeline {pipeline_id}"
        in response.json()["detail"]
    )


def test_list_pipeline_jobs_endpoint(mock_get_db, sample_pipeline, mock_jobs):
    test_client = TestClient(app)

    pipeline_id = sample_pipeline.id

    mock_get_db.get.return_value = sample_pipeline

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_jobs
    mock_get_db.execute.return_value = mock_result

    for job in mock_jobs:
        job.pipeline_id = pipeline_id

    response = test_client.get(f"/api/pipelines/{pipeline_id}/jobs")

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert response.json()[0]["job_type"] == "validate_manifest"
    assert response.json()[0]["position"] == 0
    assert response.json()[1]["job_type"] == "build"
    assert response.json()[1]["position"] == 1


def test_list_pipeline_jobs_pipeline_not_found(mock_get_db):
    test_client = TestClient(app)

    pipeline_id = uuid.uuid4()

    mock_get_db.get.return_value = None

    response = test_client.get(f"/api/pipelines/{pipeline_id}/jobs")

    assert response.status_code == 404
    assert f"Pipeline {pipeline_id} not found" in response.json()["detail"]
