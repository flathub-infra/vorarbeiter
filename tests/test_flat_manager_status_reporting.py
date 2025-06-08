import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.github_notifier import GitHubNotifier
from app.services.job_monitor import JobMonitor
from app.models import Pipeline, PipelineStatus
from app.utils.flat_manager import JobStatus
import uuid


@pytest.fixture
def mock_pipeline():
    pipeline = MagicMock(spec=Pipeline)
    pipeline.id = uuid.uuid4()
    pipeline.app_id = "org.example.App"
    pipeline.params = {"sha": "abc123def456", "repo": "flathub/org.example.App"}
    pipeline.commit_job_id = 12345
    pipeline.publish_job_id = 12346
    pipeline.update_repo_job_id = 12347
    pipeline.build_id = 99999
    pipeline.flat_manager_repo = "stable"
    return pipeline


@pytest.mark.asyncio
async def test_notify_flat_manager_job_status():
    github_notifier = GitHubNotifier()
    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.params = {"sha": "abc123", "repo": "flathub/org.example.App"}

    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_flat_manager_job_status(
            mock_pipeline, "commit", 12345, "pending", "Committing build..."
        )

        mock_update.assert_called_once_with(
            sha="abc123",
            state="pending",
            git_repo="flathub/org.example.App",
            description="Committing build...",
            target_url="https://hub.flathub.org/status/12345",
            context="flat-manager/commit",
        )


@pytest.mark.asyncio
async def test_job_monitor_commit_job_success(mock_pipeline):
    job_monitor = JobMonitor()

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
                    db, mock_pipeline
                )

                assert result is True
                assert mock_pipeline.status == PipelineStatus.COMMITTED
                mock_notify_completed.assert_called_once_with(
                    mock_pipeline, "commit", 12345, success=True
                )
                mock_notify_committed.assert_called_once()


@pytest.mark.asyncio
async def test_job_monitor_commit_job_failure(mock_pipeline):
    job_monitor = JobMonitor()

    mock_job_response = {"status": JobStatus.BROKEN, "kind": 0, "results": ""}

    with patch.object(
        job_monitor.flat_manager, "get_job", return_value=mock_job_response
    ):
        with patch.object(
            job_monitor, "_notify_flat_manager_job_completed"
        ) as mock_notify_completed:
            db = AsyncMock()
            result = await job_monitor._process_succeeded_pipeline(db, mock_pipeline)

            assert result is True
            assert mock_pipeline.status == PipelineStatus.FAILED
            mock_notify_completed.assert_called_once_with(
                mock_pipeline, "commit", 12345, success=False
            )


@pytest.mark.asyncio
async def test_job_monitor_publish_job_with_update_repo(mock_pipeline):
    job_monitor = JobMonitor()
    mock_pipeline.status = PipelineStatus.COMMITTED

    mock_job_response = {
        "status": JobStatus.ENDED,
        "kind": 1,  # PUBLISH
        "results": '{"update-repo-job": 12347}',
    }

    with patch.object(
        job_monitor.flat_manager, "get_job", return_value=mock_job_response
    ):
        with patch.object(
            job_monitor, "_notify_flat_manager_job_completed"
        ) as mock_notify_completed:
            with patch.object(
                job_monitor, "_notify_flat_manager_job_started"
            ) as mock_notify_started:
                db = AsyncMock()
                result = await job_monitor._process_publish_job(db, mock_pipeline)

                assert result is True
                assert mock_pipeline.status == PipelineStatus.PUBLISHING
                assert mock_pipeline.update_repo_job_id == 12347
                mock_notify_completed.assert_called_once_with(
                    mock_pipeline, "publish", 12346, success=True
                )
                mock_notify_started.assert_called_once_with(
                    mock_pipeline, "update-repo", 12347
                )


@pytest.mark.asyncio
async def test_job_monitor_update_repo_job_success(mock_pipeline):
    job_monitor = JobMonitor()
    mock_pipeline.status = PipelineStatus.PUBLISHING

    mock_job_response = {
        "status": JobStatus.ENDED,
        "kind": 2,  # UPDATE_REPO
        "results": "",
    }

    with patch.object(
        job_monitor.flat_manager, "get_job", return_value=mock_job_response
    ):
        with patch.object(
            job_monitor, "_notify_flat_manager_job_completed"
        ) as mock_notify_completed:
            db = AsyncMock()
            result = await job_monitor._process_update_repo_job(db, mock_pipeline)

            assert result is True
            assert mock_pipeline.status == PipelineStatus.PUBLISHED
            assert mock_pipeline.published_at is not None
            mock_notify_completed.assert_called_once_with(
                mock_pipeline, "update-repo", 12347, success=True
            )


@pytest.mark.asyncio
async def test_build_pipeline_sets_initial_commit_status():
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
    mock_pipeline.flat_manager_repo = "stable"

    build_pipeline = BuildPipeline()

    with patch("app.pipelines.build.get_db") as mock_get_db:
        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.get.return_value = mock_pipeline
        mock_get_db.return_value = mock_db

        # Mock FlatManagerClient for commit flow
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

                mock_notifier.notify_flat_manager_job_status.assert_called_once_with(
                    mock_pipeline, "commit", 12345, "pending", "Committing build..."
                )


@pytest.mark.asyncio
async def test_job_monitor_fetches_and_notifies_new_commit_job(mock_pipeline):
    job_monitor = JobMonitor()
    mock_pipeline.commit_job_id = None
    mock_pipeline.publish_job_id = None
    mock_pipeline.flat_manager_repo = "stable"

    with patch.object(
        job_monitor.flat_manager, "get_build_info"
    ) as mock_get_build_info:
        mock_get_build_info.return_value = {
            "build": {"commit_job_id": 12345, "publish_job_id": 12346}
        }

        with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
            mock_get_job.return_value = {"status": JobStatus.NEW}

            with patch.object(
                job_monitor, "_notify_flat_manager_job_new"
            ) as mock_notify_new:
                result = await job_monitor._fetch_missing_job_ids(mock_pipeline)

                assert result is True
                assert mock_pipeline.commit_job_id == 12345
                assert mock_pipeline.publish_job_id == 12346
                mock_notify_new.assert_any_call(mock_pipeline, "commit", 12345)
                mock_notify_new.assert_any_call(mock_pipeline, "publish", 12346)


@pytest.mark.asyncio
async def test_job_monitor_skips_notification_for_non_new_jobs(mock_pipeline):
    job_monitor = JobMonitor()
    mock_pipeline.commit_job_id = None
    mock_pipeline.flat_manager_repo = "stable"

    with patch.object(
        job_monitor.flat_manager, "get_build_info"
    ) as mock_get_build_info:
        mock_get_build_info.return_value = {"build": {"commit_job_id": 12345}}

        with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
            mock_get_job.return_value = {"status": JobStatus.STARTED}

            with patch.object(
                job_monitor, "_notify_flat_manager_job_new"
            ) as mock_notify_new:
                result = await job_monitor._fetch_missing_job_ids(mock_pipeline)

                assert result is True
                assert mock_pipeline.commit_job_id == 12345
                mock_notify_new.assert_not_called()


@pytest.mark.asyncio
async def test_notify_flat_manager_job_new():
    job_monitor = JobMonitor()
    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.params = {"sha": "abc123", "repo": "flathub/org.example.App"}
    mock_pipeline.flat_manager_repo = "stable"

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier = AsyncMock()
        mock_notifier_class.return_value = mock_notifier

        await job_monitor._notify_flat_manager_job_new(mock_pipeline, "commit", 12345)

        mock_notifier.notify_flat_manager_job_status.assert_called_once_with(
            mock_pipeline, "commit", 12345, "pending", "Commit job queued"
        )


@pytest.mark.asyncio
async def test_notify_flat_manager_job_new_skips_test_builds():
    job_monitor = JobMonitor()
    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.flat_manager_repo = "test"

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier = AsyncMock()
        mock_notifier_class.return_value = mock_notifier

        await job_monitor._notify_flat_manager_job_new(mock_pipeline, "commit", 12345)

        mock_notifier.notify_flat_manager_job_status.assert_not_called()
