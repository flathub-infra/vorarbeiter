import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineStatus


@pytest.mark.asyncio
async def test_check_jobs_endpoint_success(client, db_session_maker, auth_headers):
    session_maker = db_session_maker

    # Create test pipelines
    pipeline1 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App1",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=12345,
        build_id=1,
        flat_manager_repo="stable",
        params={},
    )

    pipeline2 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App2",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=12346,
        build_id=2,
        flat_manager_repo="test",
        params={},
    )

    pipeline3 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App3",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        build_id=3,
        flat_manager_repo="stable",
        params={},
    )

    pipeline4 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App4",
        status=PipelineStatus.COMMITTED,
        commit_job_id=12347,
        build_id=4,
        flat_manager_repo="stable",
        params={},
    )

    async with session_maker() as session:
        session.add_all([pipeline1, pipeline2, pipeline3, pipeline4])
        await session.commit()

    with patch("app.services.job_monitor.FlatManagerClient") as mock_fm_class:
        mock_fm_instance = AsyncMock()
        mock_fm_class.return_value = mock_fm_instance

        mock_fm_instance.get_job.side_effect = [
            {"status": 2},
            {"status": 1},
        ]

        response = client.post(
            "/api/pipelines/check-jobs",
            headers=auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["checked_pipelines"] == 2
        assert result["updated_pipelines"] == 1

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline1.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_check_jobs_endpoint_unauthorized(client):
    response = client.post(
        "/api/pipelines/check-jobs",
        headers={"Authorization": "Bearer wrong_token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API token"


@pytest.mark.asyncio
async def test_check_jobs_endpoint_no_pipelines(client, db_session_maker, auth_headers):
    response = client.post(
        "/api/pipelines/check-jobs",
        headers=auth_headers,
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    assert result["checked_pipelines"] == 0
    assert result["updated_pipelines"] == 0


@pytest.mark.asyncio
async def test_check_jobs_endpoint_all_failed(client, db_session_maker, auth_headers):
    session_maker = db_session_maker

    # Create test pipeline
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=12345,
        build_id=1,
        flat_manager_repo="stable",
        params={},
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.services.job_monitor.FlatManagerClient") as mock_fm_class:
        mock_fm_instance = AsyncMock()
        mock_fm_class.return_value = mock_fm_instance

        mock_fm_instance.get_job.return_value = {"status": 3}

        response = client.post(
            "/api/pipelines/check-jobs",
            headers=auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["checked_pipelines"] == 1
        assert result["updated_pipelines"] == 1

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.status == PipelineStatus.FAILED
