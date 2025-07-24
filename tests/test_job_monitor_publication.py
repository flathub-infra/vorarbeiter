import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Pipeline, PipelineStatus
from app.services.job_monitor import JobMonitor
from app.utils.flat_manager import JobKind, JobStatus


@pytest.fixture
def job_monitor():
    return JobMonitor()


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def stable_pipeline_publishing():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHING,
        publish_job_id=67890,
        update_repo_job_id=99999,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )


@pytest.fixture
def beta_pipeline_publishing():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHING,
        publish_job_id=67890,
        update_repo_job_id=99999,
        build_id=123,
        flat_manager_repo="beta",
        params={},
    )


@pytest.mark.asyncio
async def test_process_update_repo_job_success_calls_handle_publication(
    job_monitor, mock_db, stable_pipeline_publishing
):
    """Test that successful update_repo_job calls BuildPipeline.handle_publication()."""
    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
        patch("app.pipelines.build.BuildPipeline") as mock_build_pipeline_class,
    ):
        # Mock flat-manager job response
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.UPDATE_REPO,
        }

        # Mock BuildPipeline instance
        mock_build_pipeline = AsyncMock()
        mock_build_pipeline_class.return_value = mock_build_pipeline

        # Call the method
        result = await job_monitor._process_update_repo_job(
            mock_db, stable_pipeline_publishing
        )

        # Verify results
        assert result is True
        assert stable_pipeline_publishing.status == PipelineStatus.PUBLISHED
        assert stable_pipeline_publishing.published_at is not None

        # Verify BuildPipeline.handle_publication was called
        mock_build_pipeline_class.assert_called_once()
        mock_build_pipeline.handle_publication.assert_called_once_with(
            stable_pipeline_publishing
        )

        # Verify notification was sent
        mock_notify.assert_called_once_with(
            stable_pipeline_publishing, "update-repo", 99999, success=True
        )


@pytest.mark.asyncio
async def test_process_update_repo_job_success_handles_publication_errors(
    job_monitor, mock_db, stable_pipeline_publishing
):
    """Test that errors in handle_publication don't fail the job monitoring."""
    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed"),
        patch("app.pipelines.build.BuildPipeline") as mock_build_pipeline_class,
    ):
        # Mock flat-manager job response
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.UPDATE_REPO,
        }

        # Mock BuildPipeline to raise exception
        mock_build_pipeline = AsyncMock()
        mock_build_pipeline.handle_publication.side_effect = Exception(
            "Publication error"
        )
        mock_build_pipeline_class.return_value = mock_build_pipeline

        # Call should not raise exception despite publication error
        result = await job_monitor._process_update_repo_job(
            mock_db, stable_pipeline_publishing
        )

        # Pipeline status should still be updated correctly
        assert result is True
        assert stable_pipeline_publishing.status == PipelineStatus.PUBLISHED

        # Verify BuildPipeline.handle_publication was called
        mock_build_pipeline.handle_publication.assert_called_once_with(
            stable_pipeline_publishing
        )


@pytest.mark.asyncio
async def test_process_update_repo_job_failed_does_not_call_handle_publication(
    job_monitor, mock_db, stable_pipeline_publishing
):
    """Test that failed update_repo_job does NOT call BuildPipeline.handle_publication()."""
    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
        patch("app.pipelines.build.BuildPipeline") as mock_build_pipeline_class,
    ):
        # Mock flat-manager job response as failed
        mock_get_job.return_value = {
            "status": JobStatus.BROKEN,
            "kind": JobKind.UPDATE_REPO,
        }

        mock_build_pipeline = AsyncMock()
        mock_build_pipeline_class.return_value = mock_build_pipeline

        result = await job_monitor._process_update_repo_job(
            mock_db, stable_pipeline_publishing
        )

        # Verify results
        assert result is True
        assert stable_pipeline_publishing.status == PipelineStatus.FAILED

        # Verify BuildPipeline.handle_publication was NOT called
        mock_build_pipeline_class.assert_not_called()
        mock_build_pipeline.handle_publication.assert_not_called()

        # Verify failure notification was sent
        mock_notify.assert_called_once_with(
            stable_pipeline_publishing, "update-repo", 99999, success=False
        )


@pytest.mark.asyncio
async def test_process_update_repo_job_still_running_does_not_call_handle_publication(
    job_monitor, mock_db, stable_pipeline_publishing
):
    """Test that running update_repo_job does NOT call BuildPipeline.handle_publication()."""
    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch("app.pipelines.build.BuildPipeline") as mock_build_pipeline_class,
    ):
        # Mock flat-manager job response as still running
        mock_get_job.return_value = {
            "status": JobStatus.NEW,
            "kind": JobKind.UPDATE_REPO,
        }

        mock_build_pipeline = AsyncMock()
        mock_build_pipeline_class.return_value = mock_build_pipeline

        result = await job_monitor._process_update_repo_job(
            mock_db, stable_pipeline_publishing
        )

        # Verify results - no status change
        assert result is False
        assert stable_pipeline_publishing.status == PipelineStatus.PUBLISHING

        # Verify BuildPipeline.handle_publication was NOT called
        mock_build_pipeline_class.assert_not_called()
        mock_build_pipeline.handle_publication.assert_not_called()
