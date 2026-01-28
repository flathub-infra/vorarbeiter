import uuid
from unittest.mock import patch

import pytest

from app.models import Pipeline, PipelineStatus
from app.services.job_monitor import JobMonitor


@pytest.fixture
def job_monitor():
    return JobMonitor()


@pytest.mark.asyncio
async def test_fetch_missing_job_ids_success(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        publish_job_id=None,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        mock_get_info.return_value = {
            "build": {"commit_job_id": 789, "publish_job_id": 101112}
        }

        result = await job_monitor._fetch_missing_job_ids(pipeline)

        assert result is True
        assert pipeline.commit_job_id == 789
        assert pipeline.publish_job_id == 101112
        mock_get_info.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_fetch_missing_job_ids_partial_update(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=456,
        publish_job_id=None,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        mock_get_info.return_value = {
            "build": {"commit_job_id": 789, "publish_job_id": 101112}
        }

        result = await job_monitor._fetch_missing_job_ids(pipeline)

        assert result is True
        assert pipeline.commit_job_id == 456  # Should not be updated
        assert pipeline.publish_job_id == 101112


@pytest.mark.asyncio
async def test_fetch_missing_job_ids_no_build_id(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        build_id=None,
        flat_manager_repo="stable",
        params={},
    )

    result = await job_monitor._fetch_missing_job_ids(pipeline)

    assert result is False
    assert pipeline.commit_job_id is None


@pytest.mark.asyncio
async def test_fetch_missing_job_ids_api_error(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        mock_get_info.side_effect = Exception("API Error")

        result = await job_monitor._fetch_missing_job_ids(pipeline)

        assert result is False
        assert pipeline.commit_job_id is None


@pytest.mark.asyncio
async def test_fetch_missing_job_ids_no_updates_needed(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=111,
        publish_job_id=222,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        mock_get_info.return_value = {
            "build": {"commit_job_id": 789, "publish_job_id": 101112}
        }

        result = await job_monitor._fetch_missing_job_ids(pipeline)

        assert result is False
        assert pipeline.commit_job_id == 111
        assert pipeline.publish_job_id == 222


@pytest.mark.asyncio
async def test_check_and_update_fetches_missing_ids_then_checks_status(
    job_monitor,
):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        publish_job_id=None,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        mock_get_info.return_value = {
            "build": {"commit_job_id": 789, "publish_job_id": 101112}
        }

        with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
            mock_get_job.side_effect = [
                {"status": 1},
                {"status": 1},
                {"status": 2},
            ]

            result = await job_monitor.check_and_update_pipeline_jobs(pipeline)

            assert result is True
            assert pipeline.commit_job_id == 789
            assert pipeline.publish_job_id == 101112
            assert pipeline.status == PipelineStatus.COMMITTED
            mock_get_info.assert_called_once_with(123)
            assert mock_get_job.call_count == 3


@pytest.mark.asyncio
async def test_fetch_missing_job_ids_test_pipeline_skip_publish(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        publish_job_id=None,
        build_id=123,
        flat_manager_repo="test",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        mock_get_info.return_value = {
            "build": {"commit_job_id": 789, "publish_job_id": 101112}
        }

        result = await job_monitor._fetch_missing_job_ids(pipeline)

        assert result is True
        assert pipeline.commit_job_id == 789
        assert pipeline.publish_job_id is None
        mock_get_info.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_check_and_update_no_fetch_if_ids_present(job_monitor):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=789,
        publish_job_id=101112,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    with patch.object(job_monitor.flat_manager, "get_build_info") as mock_get_info:
        with patch.object(job_monitor.flat_manager, "get_job") as mock_get_job:
            mock_get_job.return_value = {"status": 1}  # STARTED

            result = await job_monitor.check_and_update_pipeline_jobs(pipeline)

            assert result is False
            mock_get_info.assert_not_called()
            mock_get_job.assert_called_once_with(789)
