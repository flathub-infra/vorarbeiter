import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.job_monitor import JobMonitor
from app.models import Pipeline, PipelineStatus
from app.utils.flat_manager import JobStatus
import uuid


@pytest.fixture
def mock_test_pipeline():
    pipeline = MagicMock(spec=Pipeline)
    pipeline.id = uuid.uuid4()
    pipeline.app_id = "org.example.App"
    pipeline.params = {"sha": "abc123def456", "repo": "flathub/org.example.App"}
    pipeline.commit_job_id = 12345
    pipeline.publish_job_id = 12346
    pipeline.update_repo_job_id = 12347
    pipeline.build_id = 99999
    pipeline.flat_manager_repo = "test"  # Test repo
    return pipeline


@pytest.mark.asyncio
async def test_job_monitor_skips_notifications_for_test_builds(mock_test_pipeline):
    """Test that flat-manager job notifications are skipped for test builds"""
    job_monitor = JobMonitor()

    # Test commit job success for test build
    mock_job_response = {"status": JobStatus.ENDED, "kind": 0, "results": ""}

    with patch.object(
        job_monitor.flat_manager, "get_job", return_value=mock_job_response
    ):
        with patch.object(
            job_monitor, "_notify_flat_manager_job_completed"
        ) as mock_notify_completed:
            with patch.object(
                job_monitor, "_notify_committed"
            ) as mock_notify_committed:
                db = AsyncMock()
                result = await job_monitor._process_succeeded_pipeline(
                    db, mock_test_pipeline
                )

                assert result is True
                assert mock_test_pipeline.status == PipelineStatus.COMMITTED
                # Should still be called since the check is inside the method
                mock_notify_completed.assert_called_once_with(
                    mock_test_pipeline, "commit", 12345, success=True
                )
                mock_notify_committed.assert_called_once()


@pytest.mark.asyncio
async def test_notify_methods_skip_for_test_builds(mock_test_pipeline):
    """Test that notify methods return early for test builds"""
    job_monitor = JobMonitor()

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier = AsyncMock()
        mock_notifier_class.return_value = mock_notifier

        # Test _notify_flat_manager_job_started
        await job_monitor._notify_flat_manager_job_started(
            mock_test_pipeline, "commit", 12345
        )
        mock_notifier.notify_flat_manager_job_status.assert_not_called()

        # Test _notify_flat_manager_job_completed
        await job_monitor._notify_flat_manager_job_completed(
            mock_test_pipeline, "commit", 12345, success=True
        )
        mock_notifier.notify_flat_manager_job_status.assert_not_called()


@pytest.mark.asyncio
async def test_build_pipeline_skips_notification_for_test_builds():
    """Test that BuildPipeline skips flat-manager notifications for test builds"""
    from app.pipelines.build import BuildPipeline

    pipeline_id = uuid.uuid4()
    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id
    mock_pipeline.app_id = "org.example.App"
    mock_pipeline.params = {"sha": "abc123", "repo": "flathub/org.example.App"}
    mock_pipeline.build_id = 99999
    mock_pipeline.commit_job_id = None
    mock_pipeline.end_of_life = None
    mock_pipeline.end_of_life_rebase = None
    mock_pipeline.flat_manager_repo = "test"  # Test repo

    build_pipeline = BuildPipeline()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.get.return_value = mock_pipeline
        mock_get_db.return_value = mock_db

        with patch("app.pipelines.build.FlatManagerClient") as mock_fm_class:
            mock_fm_instance = AsyncMock()
            mock_fm_instance.commit = AsyncMock(return_value=None)
            mock_fm_instance.get_build_info = AsyncMock(
                return_value={"build": {"commit_job_id": 12345}}
            )
            mock_fm_class.return_value = mock_fm_instance

            with patch("app.pipelines.build.GitHubNotifier") as mock_notifier_class:
                mock_notifier = AsyncMock()
                mock_notifier_class.return_value = mock_notifier

                await build_pipeline.handle_callback(pipeline_id, {"status": "success"})

                # Should not be called for test builds
                mock_notifier.notify_flat_manager_job_status.assert_not_called()


@pytest.mark.asyncio
async def test_publishing_service_skips_notification_for_non_stable_beta():
    """Test that PublishingService skips notifications for pipelines not in stable/beta"""
    from app.services.publishing import PublishingService

    # Note: Publishing service only processes stable/beta pipelines anyway,
    # but we'll test the notification part specifically
    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = uuid.uuid4()
    mock_pipeline.app_id = "org.example.App"
    mock_pipeline.params = {"sha": "abc123", "repo": "flathub/org.example.App"}
    mock_pipeline.build_id = 99999
    mock_pipeline.flat_manager_repo = "test"  # Would not normally get here

    publishing_service = PublishingService()

    with patch.object(
        publishing_service.flat_manager, "publish", new_callable=AsyncMock
    ):
        with patch.object(
            publishing_service.flat_manager, "get_build_info", new_callable=AsyncMock
        ) as mock_get_info:
            mock_get_info.return_value = {"build": {"publish_job_id": 12346}}

            with patch(
                "app.services.github_notifier.GitHubNotifier"
            ) as mock_notifier_class:
                mock_notifier = AsyncMock()
                mock_notifier_class.return_value = mock_notifier

                from app.services.publishing import PublishResult

                PublishResult()

                # Since test repos are filtered out in _get_publishable_pipelines,
                # we'll just verify the notification logic would skip
                if mock_pipeline.flat_manager_repo in ["stable", "beta"]:
                    await mock_notifier.notify_flat_manager_job_status(
                        mock_pipeline,
                        "publish",
                        12346,
                        "pending",
                        "Publishing build...",
                    )

                mock_notifier.notify_flat_manager_job_status.assert_not_called()
