import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineStatus


@pytest.mark.asyncio
async def test_check_jobs_endpoint_fetches_missing_ids(
    client, db_session_maker, auth_headers
):
    session_maker = db_session_maker

    # Create pipelines with missing job IDs
    pipeline1 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App1",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        publish_job_id=None,
        build_id=123,
        flat_manager_repo="stable",
        params={},
    )

    pipeline2 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App2",
        status=PipelineStatus.COMMITTED,
        commit_job_id=456,
        publish_job_id=None,
        build_id=124,
        flat_manager_repo="stable",
        params={},
    )

    async with session_maker() as session:
        session.add_all([pipeline1, pipeline2])
        await session.commit()

    with patch("app.services.job_monitor.FlatManagerClient") as mock_fm_class:
        mock_fm_instance = AsyncMock()
        mock_fm_class.return_value = mock_fm_instance

        # Mock get_build_info to return job IDs
        mock_fm_instance.get_build_info.side_effect = [
            {"build": {"commit_job_id": 789, "publish_job_id": 101112}},
            {"build": {"commit_job_id": 456, "publish_job_id": 131415}},
        ]

        # Mock get_job to check job status
        mock_fm_instance.get_job.return_value = {"status": 1}  # STARTED

        response = client.post(
            "/api/pipelines/check-jobs",
            headers=auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["checked_pipelines"] == 2
        assert result["updated_pipelines"] == 2

        # Verify job IDs were fetched and saved
        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline1.id)
            db_result = await session.execute(query)
            updated_pipeline1 = db_result.scalars().first()
            assert updated_pipeline1.commit_job_id == 789
            assert updated_pipeline1.publish_job_id == 101112

            query = select(Pipeline).where(Pipeline.id == pipeline2.id)
            db_result = await session.execute(query)
            updated_pipeline2 = db_result.scalars().first()
            assert updated_pipeline2.commit_job_id == 456
            assert updated_pipeline2.publish_job_id == 131415


@pytest.mark.asyncio
async def test_check_jobs_endpoint_handles_partial_missing_ids(
    client, db_session_maker, auth_headers
):
    session_maker = db_session_maker

    # Pipeline with only commit_job_id missing
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        publish_job_id=999,
        build_id=125,
        flat_manager_repo="stable",
        params={},
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.services.job_monitor.FlatManagerClient") as mock_fm_class:
        mock_fm_instance = AsyncMock()
        mock_fm_class.return_value = mock_fm_instance

        # Mock get_build_info
        mock_fm_instance.get_build_info.return_value = {
            "build": {"commit_job_id": 888, "publish_job_id": 999}
        }

        response = client.post(
            "/api/pipelines/check-jobs",
            headers=auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["checked_pipelines"] == 1
        assert result["updated_pipelines"] == 1

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.commit_job_id == 888
            assert updated_pipeline.publish_job_id == 999


@pytest.mark.asyncio
async def test_check_jobs_endpoint_handles_fetch_error(
    client, db_session_maker, auth_headers
):
    session_maker = db_session_maker

    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        build_id=126,
        flat_manager_repo="stable",
        params={},
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.services.job_monitor.FlatManagerClient") as mock_fm_class:
        mock_fm_instance = AsyncMock()
        mock_fm_class.return_value = mock_fm_instance

        # Mock get_build_info to raise an error
        mock_fm_instance.get_build_info.side_effect = Exception("API Error")

        response = client.post(
            "/api/pipelines/check-jobs",
            headers=auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["checked_pipelines"] == 1
        assert result["updated_pipelines"] == 0

        # Verify job IDs were not updated
        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.commit_job_id is None
