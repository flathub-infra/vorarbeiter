import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.schemas.pipelines import PipelineType
from app.services.pipeline import PipelineService


@pytest.fixture
def pipeline_service():
    return PipelineService()


@pytest.fixture
def mock_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"branch": "main"},
        triggered_by=PipelineTrigger.MANUAL,
        build_id=123,
        flat_manager_repo="stable",
        created_at=datetime.now(),
        repro_pipeline_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_get_pipeline_with_job_updates_not_found(pipeline_service):
    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.get.return_value = None

    result = await pipeline_service.get_pipeline_with_job_updates(mock_db, uuid.uuid4())

    assert result is None
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_get_pipeline_with_job_updates_returns_pipeline(
    pipeline_service, mock_pipeline
):
    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.get.return_value = mock_pipeline

    result = await pipeline_service.get_pipeline_with_job_updates(
        mock_db, mock_pipeline.id
    )

    assert result == mock_pipeline
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_list_pipelines_with_filters_basic(pipeline_service):
    pipelines = [
        MagicMock(id=uuid.uuid4(), commit_job_id=1, publish_job_id=2),
        MagicMock(id=uuid.uuid4(), commit_job_id=3, publish_job_id=4),
    ]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = pipelines

    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.execute.return_value = mock_result

    result = await pipeline_service.list_pipelines_with_filters(mock_db)

    assert result == pipelines
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_list_pipelines_with_filters_all_params(pipeline_service):
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.execute.return_value = mock_result

    result = await pipeline_service.list_pipelines_with_filters(
        mock_db,
        app_id="org.test",
        status=PipelineStatus.SUCCEEDED,
        triggered_by=PipelineTrigger.WEBHOOK,
        target_repo="stable",
        limit=50,
    )

    assert result == []
    mock_db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_list_pipelines_with_filters_limit_bounds(pipeline_service):
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_db = AsyncMock(spec=AsyncSession)
    mock_db.execute.return_value = mock_result

    await pipeline_service.list_pipelines_with_filters(mock_db, limit=0)
    await pipeline_service.list_pipelines_with_filters(mock_db, limit=200)

    assert mock_db.execute.call_count == 2


def test_pipeline_to_summary(pipeline_service, mock_pipeline):
    summary = pipeline_service.pipeline_to_summary(mock_pipeline)

    assert summary.id == str(mock_pipeline.id)
    assert summary.app_id == mock_pipeline.app_id
    assert summary.type == PipelineType.BUILD
    assert summary.status == mock_pipeline.status
    assert summary.repo == mock_pipeline.flat_manager_repo
    assert summary.triggered_by == mock_pipeline.triggered_by
    assert summary.build_id == mock_pipeline.build_id
    assert summary.repro_pipeline_id == mock_pipeline.repro_pipeline_id


def test_pipeline_to_summary_no_repo(pipeline_service, mock_pipeline):
    mock_pipeline.flat_manager_repo = None

    summary = pipeline_service.pipeline_to_summary(mock_pipeline)

    assert summary.repo is None


def test_pipeline_to_response(pipeline_service, mock_pipeline):
    mock_pipeline.log_url = "http://example.com/log"

    response = pipeline_service.pipeline_to_response(mock_pipeline)

    assert response.id == str(mock_pipeline.id)
    assert response.app_id == mock_pipeline.app_id
    assert response.status == mock_pipeline.status
    assert response.repo == mock_pipeline.flat_manager_repo
    assert response.params == mock_pipeline.params
    assert response.log_url == mock_pipeline.log_url
    assert response.repro_pipeline_id == mock_pipeline.repro_pipeline_id


def test_validate_status_valid(pipeline_service):
    result = pipeline_service.validate_status("succeeded")
    assert result == PipelineStatus.SUCCEEDED

    result = pipeline_service.validate_status("pending")
    assert result == PipelineStatus.PENDING


def test_validate_status_invalid(pipeline_service):
    with pytest.raises(ValueError) as exc_info:
        pipeline_service.validate_status("invalid_status")

    assert "Invalid status value" in str(exc_info.value)
    assert "invalid_status" in str(exc_info.value)


def test_validate_trigger_filter_valid(pipeline_service):
    result = pipeline_service.validate_trigger_filter("manual")
    assert result == PipelineTrigger.MANUAL

    result = pipeline_service.validate_trigger_filter("webhook")
    assert result == PipelineTrigger.WEBHOOK


def test_validate_trigger_filter_invalid(pipeline_service):
    with pytest.raises(ValueError) as exc_info:
        pipeline_service.validate_trigger_filter("invalid_trigger")

    assert "Invalid triggered_by value" in str(exc_info.value)
    assert "invalid_trigger" in str(exc_info.value)


@pytest.mark.asyncio
async def test_trigger_manual_pipeline_success(pipeline_service):
    app_id = "org.test.App"
    params = {"branch": "main"}
    pipeline_id = uuid.uuid4()

    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id
    mock_pipeline.app_id = app_id
    mock_pipeline.status = PipelineStatus.RUNNING
    mock_pipeline.triggered_by = PipelineTrigger.MANUAL

    with patch("app.pipelines.BuildPipeline") as MockBuildPipeline:
        mock_build = AsyncMock()
        mock_build.create_pipeline.return_value = mock_pipeline
        mock_build.start_pipeline.return_value = mock_pipeline
        MockBuildPipeline.return_value = mock_build

        with patch("app.database.get_db") as mock_get_db:
            mock_db = AsyncMock()
            mock_db.get.return_value = mock_pipeline
            mock_get_db.return_value.__aenter__.return_value = mock_db

            result = await pipeline_service.trigger_manual_pipeline(app_id, params)

            assert result["status"] == "created"
            assert result["pipeline_id"] == str(pipeline_id)
            assert result["app_id"] == app_id
            assert result["pipeline_status"] == "running"

            mock_build.create_pipeline.assert_called_once_with(
                app_id=app_id, params=params, webhook_event_id=None
            )
            mock_build.start_pipeline.assert_called_once_with(pipeline_id=pipeline_id)


@pytest.mark.asyncio
async def test_trigger_manual_pipeline_not_found(pipeline_service):
    app_id = "org.test.App"
    params = {"branch": "main"}
    pipeline_id = uuid.uuid4()

    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id

    with patch("app.pipelines.BuildPipeline") as MockBuildPipeline:
        mock_build = AsyncMock()
        mock_build.create_pipeline.return_value = mock_pipeline
        MockBuildPipeline.return_value = mock_build

        with patch("app.database.get_db") as mock_get_db:
            mock_db = AsyncMock()
            mock_db.get.return_value = None
            mock_get_db.return_value.__aenter__.return_value = mock_db

            with pytest.raises(ValueError) as exc_info:
                await pipeline_service.trigger_manual_pipeline(app_id, params)

            assert f"Pipeline {pipeline_id} not found" in str(exc_info.value)


def test_pipeline_to_summary_reprocheck_type(pipeline_service):
    """Test that reprocheck pipelines get correct type."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"workflow_id": "reprocheck.yml"},
        triggered_by=PipelineTrigger.MANUAL,
        flat_manager_repo=None,
        created_at=datetime.now(),
    )

    summary = pipeline_service.pipeline_to_summary(pipeline)

    assert summary.type == PipelineType.REPROCHECK


def test_pipeline_to_summary_build_type_explicit(pipeline_service):
    """Test that build pipelines with explicit workflow_id get correct type."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"workflow_id": "build.yml"},
        triggered_by=PipelineTrigger.MANUAL,
        flat_manager_repo="stable",
        created_at=datetime.now(),
    )

    summary = pipeline_service.pipeline_to_summary(pipeline)

    assert summary.type == PipelineType.BUILD


def test_pipeline_to_summary_build_type_no_workflow_id(pipeline_service):
    """Test that pipelines without workflow_id default to build type."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"branch": "main"},
        triggered_by=PipelineTrigger.MANUAL,
        flat_manager_repo="stable",
        created_at=datetime.now(),
    )

    summary = pipeline_service.pipeline_to_summary(pipeline)

    assert summary.type == PipelineType.BUILD
