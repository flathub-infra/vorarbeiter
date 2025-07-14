import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Pipeline, PipelineStatus
from app.services.job_monitor import JobMonitor
from app.utils.flat_manager import JobKind, JobStatus


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
async def test_process_succeeded_pipeline_sends_pr_comment(
    job_monitor, mock_pipeline, mock_db
):
    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(
            job_monitor, "_notify_flat_manager_job_completed"
        ) as mock_notify_job,
        patch.object(job_monitor, "_notify_committed") as mock_notify_committed,
    ):
        mock_get_job.return_value = {"status": JobStatus.ENDED}

        result = await job_monitor._process_succeeded_pipeline(mock_db, mock_pipeline)

        assert result is True
        assert mock_pipeline.status == PipelineStatus.COMMITTED
        mock_notify_job.assert_called_once_with(
            mock_pipeline, "commit", 12345, success=True
        )
        mock_notify_committed.assert_called_once_with(mock_pipeline)


@pytest.mark.asyncio
async def test_check_and_update_pipeline_jobs_commit_failed(
    job_monitor, mock_pipeline, mock_db
):
    job_response = {"status": JobStatus.BROKEN, "log": "Error: commit failed"}

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_create_job_failure_issue") as mock_create_issue,
    ):
        mock_get_job.return_value = job_response

        result = await job_monitor.check_and_update_pipeline_jobs(
            mock_db, mock_pipeline
        )

        assert result is True
        assert mock_pipeline.status == PipelineStatus.FAILED
        mock_create_issue.assert_called_once_with(
            mock_pipeline, "commit", 12345, job_response
        )


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


@pytest.mark.asyncio
async def test_check_published_pipeline_jobs_with_publish_job_success(
    job_monitor, mock_db
):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=67890,
        flat_manager_repo="stable",
        params={},
    )

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
    ):
        mock_get_job.return_value = {"status": JobStatus.ENDED}
        mock_notify.return_value = None

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        mock_get_job.assert_called_once_with(67890)
        mock_notify.assert_called_once_with(pipeline, "publish", 67890, success=True)


@pytest.mark.asyncio
async def test_check_published_pipeline_jobs_with_publish_job_failed(
    job_monitor, mock_db
):
    """Test that published pipelines with failed publish_job_id get reported correctly"""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=67890,
        flat_manager_repo="stable",
        params={},
    )

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
    ):
        mock_get_job.return_value = {"status": JobStatus.BROKEN}
        mock_notify.return_value = None

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        mock_get_job.assert_called_once_with(67890)
        mock_notify.assert_called_once_with(pipeline, "publish", 67890, success=False)


@pytest.mark.asyncio
async def test_check_published_pipeline_jobs_with_update_repo_job_success(
    job_monitor, mock_db
):
    """Test that published pipelines with update_repo_job_id get reported correctly"""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        update_repo_job_id=99999,
        flat_manager_repo="beta",
        params={},
    )

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
    ):
        mock_get_job.return_value = {"status": JobStatus.ENDED}
        mock_notify.return_value = None

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        mock_get_job.assert_called_once_with(99999)
        mock_notify.assert_called_once_with(
            pipeline, "update-repo", 99999, success=True
        )


@pytest.mark.asyncio
async def test_check_published_pipeline_jobs_with_both_jobs(job_monitor, mock_db):
    """Test that published pipelines with both job IDs get both reported"""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=67890,
        update_repo_job_id=99999,
        flat_manager_repo="stable",
        params={},
    )

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
    ):
        mock_get_job.side_effect = [
            {"status": JobStatus.ENDED},  # publish job
            {"status": JobStatus.ENDED},  # update-repo job
        ]
        mock_notify.return_value = None

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert mock_get_job.call_count == 2
        mock_get_job.assert_any_call(67890)
        mock_get_job.assert_any_call(99999)

        assert mock_notify.call_count == 2
        mock_notify.assert_any_call(pipeline, "publish", 67890, success=True)
        mock_notify.assert_any_call(pipeline, "update-repo", 99999, success=True)


@pytest.mark.asyncio
async def test_check_published_pipeline_jobs_skips_test_repo(job_monitor, mock_db):
    """Test that published pipelines in test repo are skipped"""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=67890,
        flat_manager_repo="test",
        params={},
    )

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
    ):
        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        mock_get_job.assert_not_called()
        mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_check_published_pipeline_jobs_handles_exception(job_monitor, mock_db):
    """Test that exceptions during job status check are handled gracefully"""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=67890,
        flat_manager_repo="stable",
        params={},
    )

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_notify_flat_manager_job_completed") as mock_notify,
    ):
        mock_get_job.side_effect = Exception("API error")

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is False
        mock_get_job.assert_called_once_with(67890)
        mock_notify.assert_not_called()


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

    job_response = {
        "status": JobStatus.BROKEN,
        "kind": JobKind.PUBLISH,
        "log": "Error: publish failed",
    }

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_create_job_failure_issue") as mock_create_issue,
    ):
        mock_get_job.return_value = job_response

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert pipeline.status == PipelineStatus.FAILED
        mock_create_issue.assert_called_once_with(
            pipeline, "publish", 67890, job_response
        )


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

    job_response = {
        "status": JobStatus.BROKEN,
        "kind": JobKind.UPDATE_REPO,
        "log": "Error: update-repo failed",
    }

    with (
        patch.object(job_monitor.flat_manager, "get_job") as mock_get_job,
        patch.object(job_monitor, "_create_job_failure_issue") as mock_create_issue,
    ):
        mock_get_job.return_value = job_response

        result = await job_monitor.check_and_update_pipeline_jobs(mock_db, pipeline)

        assert result is True
        assert pipeline.status == PipelineStatus.FAILED
        mock_create_issue.assert_not_called()


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


@pytest.mark.asyncio
async def test_create_job_failure_issue_success(job_monitor, mock_pipeline):
    job_response = {"id": 12345, "log": "Error: job failed"}

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier_instance = MagicMock()
        mock_notifier_class.return_value = mock_notifier_instance

        await job_monitor._create_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        mock_notifier_class.assert_called_once_with()
        mock_notifier_instance.create_stable_job_failure_issue.assert_called_once_with(
            mock_pipeline, "commit", 12345, job_response
        )


@pytest.mark.asyncio
async def test_create_job_failure_issue_exception(job_monitor, mock_pipeline):
    job_response = {"id": 12345, "log": "Error: job failed"}

    with patch("app.services.github_notifier.GitHubNotifier") as mock_notifier_class:
        mock_notifier_class.side_effect = Exception("GitHub API error")

        await job_monitor._create_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )
