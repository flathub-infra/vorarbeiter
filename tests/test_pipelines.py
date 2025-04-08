import uuid
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, Job, JobStatus
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

    # Add validation result for testing
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
