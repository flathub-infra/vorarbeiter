import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Pipeline, PipelineStatus
from app.services.job_monitor import JobMonitor
from app.utils.flat_manager import JobStatus, JobKind


@pytest.fixture
def job_monitor():
    return JobMonitor()


@pytest.fixture
def mock_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=12345,
        build_id=123,
        params={"pr_number": "42", "repo": "flathub/org.test.App"},
    )


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_succeeded_to_committed(
    job_monitor, mock_pipeline, mock_db
):
    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {"status": JobStatus.ENDED}

        result = await job_monitor.check_and_update_pipeline_jobs(
            mock_db, mock_pipeline
        )

        assert result is True
        assert mock_pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_commit_failed(
    job_monitor, mock_pipeline, mock_db
):
    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {"status": JobStatus.BROKEN}

        result = await job_monitor.check_and_update_pipeline_jobs(
            mock_db, mock_pipeline
        )

        assert result is True
        assert mock_pipeline.status == PipelineStatus.FAILED


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_still_running(
    job_monitor, mock_pipeline, mock_db
):
    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {"status": JobStatus.STARTED}

        result = await job_monitor.check_and_update_pipeline_jobs(
            mock_db, mock_pipeline
        )

        assert result is False
        assert mock_pipeline.status == PipelineStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_no_commit_job_id(
    job_monitor, mock_pipeline, mock_db
):
    mock_pipeline.commit_job_id = None

    result = await job_monitor.check_and_update_pipeline_jobs(mock_db, mock_pipeline)

    assert result is False
    assert mock_pipeline.status == PipelineStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_wrong_status(
    job_monitor, mock_pipeline, mock_db
):
    mock_pipeline.status = PipelineStatus.RUNNING

    result = await job_monitor.check_and_update_pipeline_jobs(mock_db, mock_pipeline)

    assert result is False
    assert mock_pipeline.status == PipelineStatus.RUNNING


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_exception(
    job_monitor, mock_pipeline, mock_db
):
    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.side_effect = Exception("API Error")

        result = await job_monitor.check_and_update_pipeline_jobs(
            mock_db, mock_pipeline
        )

        assert result is False
        assert mock_pipeline.status == PipelineStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_check_commit_job_status_success(job_monitor):
    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {"status": JobStatus.ENDED}

        result = await job_monitor.check_commit_job_status(12345)

        assert result == JobStatus.ENDED
        mock_get_job.assert_called_once_with(12345)


@pytest.mark.asyncio
async def test_check_commit_job_status_exception(job_monitor):
    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.side_effect = Exception("API Error")

        result = await job_monitor.check_commit_job_status(12345)

        assert result is None


@pytest.mark.asyncio
async def test_notify_committed_with_pr(job_monitor, mock_pipeline):
    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier_instance = MagicMock()
        mock_notifier_class.return_value = mock_notifier_instance

        await job_monitor._notify_committed(mock_pipeline)

        mock_notifier_class.assert_called_once()
        mock_notifier_instance.handle_build_committed.assert_called_once_with(
            mock_pipeline,
            flat_manager_client=mock_notifier_class.call_args[1]["flat_manager_client"],
        )


@pytest.mark.asyncio
async def test_notify_committed_without_pr(job_monitor, mock_pipeline):
    mock_pipeline.params = {}

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier_instance = MagicMock()
        mock_notifier_class.return_value = mock_notifier_instance

        await job_monitor._notify_committed(mock_pipeline)

        mock_notifier_class.assert_called_once_with(flat_manager_client=None)
        mock_notifier_instance.handle_build_committed.assert_called_once_with(
            mock_pipeline, flat_manager_client=None
        )


@pytest.mark.asyncio
async def test_notify_committed_exception(job_monitor, mock_pipeline):
    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier_class.side_effect = Exception("Notification error")

        await job_monitor._notify_committed(mock_pipeline)


@pytest.mark.asyncio
async def test_process_publish_job_success(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,
        publish_job_id=67890,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.PUBLISH,
            "results": '{"update-repo-job": 99999}',
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert pipeline.update_repo_job_id == 99999
        assert pipeline.status == PipelineStatus.PUBLISHING


@pytest.mark.asyncio
async def test_process_publish_job_failed(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,
        publish_job_id=67890,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.BROKEN,
            "kind": JobKind.PUBLISH,
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert pipeline.status == PipelineStatus.FAILED


@pytest.mark.asyncio
async def test_process_publish_job_no_update_repo_id(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,
        publish_job_id=67890,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.PUBLISH,
            "results": "{}",
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        assert pipeline.update_repo_job_id is None
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_process_publish_job_invalid_json(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,
        publish_job_id=67890,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.PUBLISH,
            "results": "invalid json",
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        assert pipeline.update_repo_job_id is None
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_process_update_repo_job_success(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHING,
        publish_job_id=67890,
        update_repo_job_id=99999,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.UPDATE_REPO,
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert pipeline.status == PipelineStatus.PUBLISHED
        assert pipeline.published_at is not None


@pytest.mark.asyncio
async def test_process_update_repo_job_failed(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHING,
        publish_job_id=67890,
        update_repo_job_id=99999,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.BROKEN,
            "kind": JobKind.UPDATE_REPO,
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert pipeline.status == PipelineStatus.FAILED


@pytest.mark.asyncio
async def test_process_update_repo_job_still_running(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHING,
        publish_job_id=67890,
        update_repo_job_id=99999,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.STARTED,
            "kind": JobKind.UPDATE_REPO,
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        assert pipeline.status == PipelineStatus.PUBLISHING


@pytest.mark.asyncio
async def test_process_wrong_job_kind_publish(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,
        publish_job_id=67890,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.COMMIT,  # Wrong kind
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_process_wrong_job_kind_update_repo(job_monitor, mock_db):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHING,
        publish_job_id=67890,
        update_repo_job_id=99999,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
        mock_get_job.return_value = {
            "status": JobStatus.ENDED,
            "kind": JobKind.PUBLISH,  # Wrong kind
        }

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        assert pipeline.status == PipelineStatus.PUBLISHING
