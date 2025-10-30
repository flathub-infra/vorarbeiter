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
    mock_db.get.return_value = reprocheck_pipeline
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()

    mock_check_result = AsyncMock()
    mock_check_result.first = lambda: (
        str(original_pipeline.id),
        None,
    )  # id, repro_pipeline_id (None means not set)
    mock_db.execute.return_value = mock_check_result

    mock_update_db = AsyncMock(spec=AsyncSession)
    mock_update_db.execute = AsyncMock()
    mock_update_db.commit = AsyncMock()

    call_count = 0

    def get_db_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AsyncMock(__aenter__=AsyncMock(return_value=mock_db))
        else:
            return AsyncMock(__aenter__=AsyncMock(return_value=mock_update_db))

    with patch("app.pipelines.build.get_db", side_effect=get_db_side_effect):
        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            {"status": "success", "build_pipeline_id": str(original_pipeline.id)},
        )

        assert mock_db.get.call_count == 1
        mock_db.get.assert_called_with(Pipeline, reprocheck_pipeline.id)

        assert mock_db.execute.call_count == 1
        check_call = mock_db.execute.call_args_list[0]
        assert "SELECT id, repro_pipeline_id FROM pipeline" in str(check_call[0][0])

        assert mock_update_db.execute.call_count == 1
        update_call = mock_update_db.execute.call_args_list[0]
        assert "UPDATE pipeline SET repro_pipeline_id" in str(update_call[0][0])

        mock_update_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_reprocheck_callback_skips_if_repro_id_already_set(
    build_pipeline, original_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback skips update if original pipeline already has repro_pipeline_id."""
    existing_repro_id = uuid.uuid4()
    original_pipeline.repro_pipeline_id = existing_repro_id

    mock_db.get.return_value = reprocheck_pipeline
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()

    # Mock the SELECT query for checking original pipeline - this time with repro_pipeline_id already set
    mock_check_result = AsyncMock()
    mock_check_result.first = lambda: (
        str(original_pipeline.id),
        str(existing_repro_id),
    )  # id, repro_pipeline_id (already set)
    mock_db.execute.return_value = mock_check_result

    with (
        patch("app.pipelines.build.get_db") as mock_get_db,
        patch("app.pipelines.build.logger") as mock_logger,
    ):
        mock_get_db.return_value.__aenter__.return_value = mock_db

        await build_pipeline.handle_reprocheck_callback(
            reprocheck_pipeline.id,
            {"status": "success", "build_pipeline_id": str(original_pipeline.id)},
        )

        assert mock_db.get.call_count == 1
        mock_db.get.assert_called_with(Pipeline, reprocheck_pipeline.id)

        assert mock_db.execute.call_count == 1
        check_call = mock_db.execute.call_args_list[0]
        assert "SELECT id, repro_pipeline_id FROM pipeline" in str(check_call[0][0])

        # Verify no UPDATE was performed since repro_pipeline_id was already set
        update_calls = [
            call
            for call in mock_logger.info.call_args_list
            if "Updated original pipeline with reprocheck pipeline ID" in str(call)
        ]
        assert len(update_calls) == 0
        skip_calls = [
            call
            for call in mock_logger.info.call_args_list
            if "Skipping repro_pipeline_id update - already set" in str(call)
        ]
        assert len(skip_calls) == 1


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
async def test_reprocheck_callback_without_build_pipeline_id_skips_update(
    build_pipeline, mock_db
):
    """Test that build_pipeline_id processing is skipped if build_pipeline_id is not in callback."""
    regular_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={"workflow_id": "reprocheck.yml"},
        callback_token="token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    mock_db.get.return_value = regular_pipeline

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        # Call reprocheck handler with callback data that has no build_pipeline_id
        await build_pipeline.handle_reprocheck_callback(
            regular_pipeline.id,
            {"status": "success"},
        )

        assert mock_db.get.call_count == 1
        mock_db.get.assert_called_once_with(Pipeline, regular_pipeline.id)


@pytest.mark.asyncio
async def test_reprocheck_callback_ignores_non_repro_pipeline(build_pipeline, mock_db):
    """Test that callbacks for non-repro workflows do not attempt updates."""
    non_repro_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={"workflow_id": "build.yml"},
        callback_token="token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    mock_db.get.return_value = non_repro_pipeline

    with (
        patch("app.pipelines.build.get_db") as mock_get_db,
        patch("app.pipelines.build.logger") as mock_logger,
    ):
        mock_get_db.return_value.__aenter__.return_value = mock_db

        await build_pipeline.handle_reprocheck_callback(
            non_repro_pipeline.id,
            {"status": "success", "build_pipeline_id": str(uuid.uuid4())},
        )

        mock_db.execute.assert_not_called()
        skip_calls = [
            entry
            for entry in mock_logger.info.call_args_list
            if "Skipping reprocheck callback for non-repro pipeline" in str(entry)
        ]
        assert len(skip_calls) == 1


@pytest.mark.asyncio
async def test_reprocheck_callback_handles_missing_original_pipeline(
    build_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback handles missing original pipeline gracefully."""
    mock_db.get.return_value = reprocheck_pipeline
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()

    # Mock the SELECT query for checking original pipeline - returns None (not found)
    mock_check_result = AsyncMock()
    mock_check_result.first = lambda: None  # Pipeline not found
    mock_db.execute.return_value = mock_check_result

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

        # Check that we logged a warning about not finding the pipeline
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "Original pipeline not found" in str(call)
        ]
        assert len(warning_calls) == 1


@pytest.mark.asyncio
async def test_reprocheck_callback_stores_full_json_output(
    build_pipeline, reprocheck_pipeline, mock_db
):
    """Test that reprocheck callback stores full JSON output in pipeline.params."""
    mock_db.get.return_value = reprocheck_pipeline
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        callback_data = {
            "status": "success",
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
    mock_db.flush = AsyncMock()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db

        callback_data = {
            "status": "success",
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
