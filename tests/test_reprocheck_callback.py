import uuid
from unittest.mock import AsyncMock, patch
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines.build import BuildPipeline


@pytest.fixture
def build_pipeline():
    return BuildPipeline()


@pytest.fixture
def original_pipeline():
    """Original pipeline that was published to stable repo."""
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={},
        flat_manager_repo="stable",
        build_id=123,
        update_repo_job_id=456,
        callback_token="original_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.fixture
def reprocheck_pipeline(original_pipeline):
    """Reprocheck pipeline that references the original pipeline."""
    return Pipeline(
        id=uuid.uuid4(),
        app_id=original_pipeline.app_id,
        status=PipelineStatus.RUNNING,
        params={
            "workflow_id": "reprocheck.yml",
            "build_pipeline_id": str(original_pipeline.id),
        },
        callback_token="reprocheck_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.fixture
def mock_db():
    return AsyncMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_reprocheck_callback_updates_original_pipeline_repro_id(
    build_pipeline, original_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback with build_pipeline_id updates original pipeline's repro_pipeline_id."""
    mock_db.get.side_effect = [reprocheck_pipeline, original_pipeline]
    mock_db.commit = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            {"status": "success", "build_pipeline_id": str(original_pipeline.id)},
        )

        assert mock_db.get.call_count == 2
        mock_db.get.assert_any_call(Pipeline, reprocheck_pipeline.id)
        mock_db.get.assert_any_call(Pipeline, original_pipeline.id)

        assert original_pipeline.repro_pipeline_id == reprocheck_pipeline.id


@pytest.mark.asyncio
async def test_reprocheck_callback_skips_if_repro_id_already_set(
    build_pipeline, original_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback skips update if original pipeline already has repro_pipeline_id."""
    original_pipeline.repro_pipeline_id = uuid.uuid4()

    mock_db.get.side_effect = [reprocheck_pipeline, original_pipeline]
    mock_db.commit = AsyncMock()

    with (
        patch("app.pipelines.build.get_db") as mock_get_db,
        patch("app.pipelines.build.logger") as mock_logger,
    ):
        mock_get_db.return_value.__aenter__.return_value = mock_db

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            {"status": "success", "build_pipeline_id": str(original_pipeline.id)},
        )

        assert mock_db.get.call_count == 2
        mock_db.get.assert_any_call(Pipeline, reprocheck_pipeline.id)
        mock_db.get.assert_any_call(Pipeline, original_pipeline.id)

        assert original_pipeline.repro_pipeline_id != reprocheck_pipeline.id
        assert mock_logger.info.call_count == 1
        update_calls = [
            call
            for call in mock_logger.info.call_args_list
            if "Updated original pipeline with reprocheck pipeline ID" in str(call)
        ]
        assert len(update_calls) == 0


@pytest.mark.asyncio
async def test_reprocheck_callback_handles_invalid_build_pipeline_id(
    build_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback handles invalid build_pipeline_id gracefully."""
    mock_db.get.return_value = reprocheck_pipeline

    with (
        patch("app.pipelines.build.get_db") as mock_get_db,
        patch("app.pipelines.build.logger") as mock_logger,
    ):
        mock_get_db.return_value.__aenter__.return_value = mock_db

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            {"status": "success", "build_pipeline_id": "invalid-uuid"},
        )

        mock_logger.error.assert_called()
        error_call = mock_logger.error.call_args
        assert "Invalid build_pipeline_id in reprocheck callback" in str(error_call)


@pytest.mark.asyncio
async def test_reprocheck_callback_only_runs_for_reprocheck_workflows(
    build_pipeline, mock_db
):
    """Test that build_pipeline_id processing only happens for reprocheck workflows."""
    regular_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={"workflow_id": "build.yml"},
        callback_token="token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    mock_db.get.return_value = regular_pipeline

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        # Call reprocheck handler with callback data
        await build_pipeline.handle_reprocheck_callback(
            regular_pipeline.id,
            {"status": "success", "build_pipeline_id": str(uuid.uuid4())},
        )

        assert mock_db.get.call_count == 1
        mock_db.get.assert_called_once_with(Pipeline, regular_pipeline.id)


@pytest.mark.asyncio
async def test_reprocheck_callback_handles_missing_original_pipeline(
    build_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback handles missing original pipeline gracefully."""
    mock_db.get.side_effect = [reprocheck_pipeline, None]
    mock_db.commit = AsyncMock()

    with (
        patch("app.pipelines.build.get_db") as mock_get_db,
        patch("app.pipelines.build.logger") as mock_logger,
    ):
        mock_get_db.return_value.__aenter__.return_value = mock_db

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            {"status": "success", "build_pipeline_id": str(uuid.uuid4())},
        )

        info_calls = [
            call
            for call in mock_logger.info.call_args_list
            if "Updated original pipeline with reprocheck pipeline ID" in str(call)
        ]
        assert len(info_calls) == 0


@pytest.mark.asyncio
async def test_reprocheck_callback_stores_full_json_output(
    build_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback stores full JSON output in pipeline.params."""
    mock_db.get.return_value = reprocheck_pipeline
    mock_db.commit = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        callback_data = {
            "status": "success",
            "build_pipeline_id": str(uuid.uuid4()),
            "status_code": "42",
            "timestamp": "2025-01-15T10:30:45.123456+00:00",
            "result_url": "https://github.com/flathub-infra/vorarbeiter/actions/runs/12345",
            "message": "Unreproducible",
        }

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            callback_data,
        )

        assert "reprocheck_result" in reprocheck_pipeline.params
        result = reprocheck_pipeline.params["reprocheck_result"]
        assert result["status_code"] == "42"
        assert result["timestamp"] == "2025-01-15T10:30:45.123456+00:00"
        assert (
            result["result_url"]
            == "https://github.com/flathub-infra/vorarbeiter/actions/runs/12345"
        )
        assert result["message"] == "Unreproducible"


@pytest.mark.asyncio
async def test_reprocheck_callback_handles_partial_json_output(
    build_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback handles partial JSON output gracefully."""
    mock_db.get.return_value = reprocheck_pipeline
    mock_db.commit = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        callback_data = {
            "status": "success",
            "build_pipeline_id": str(uuid.uuid4()),
            "status_code": "0",
        }

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            callback_data,
        )

        assert "reprocheck_result" in reprocheck_pipeline.params
        result = reprocheck_pipeline.params["reprocheck_result"]
        assert result["status_code"] == "0"
        assert "timestamp" not in result
        assert "result_url" not in result
        assert "message" not in result
