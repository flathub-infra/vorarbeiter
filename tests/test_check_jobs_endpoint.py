import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineStatus


@pytest.mark.asyncio
async def test_check_jobs_endpoint_success(client, db_session_maker, auth_headers):
    session_maker = db_session_maker

    pipeline1 = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App1",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=12345,
        publish_job_id=12350,
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
        publish_job_id=12348,
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

        mock_fm_instance.get_build_info = AsyncMock(
            return_value={"build": {"commit_job_id": 12349, "publish_job_id": 12350}}
        )

        with patch("app.services.job_monitor.JobMonitor._notify_committed"):
            with patch(
                "app.services.job_monitor.JobMonitor._notify_flat_manager_job_completed"
            ):
                with patch(
                    "app.services.job_monitor.JobMonitor._notify_flat_manager_job_started"
                ):
                    mock_fm_instance.get_job = AsyncMock(
                        side_effect=[
                            {"status": 2},
                            {"status": 1},
                            {"status": 1},
                            {
                                "status": 2,
                                "kind": 1,
                                "results": '{"update-repo-job": 99999}',
                            },
                        ]
                    )

                    response = client.post(
                        "/api/pipelines/check-jobs",
                        headers=auth_headers,
                    )

                    assert response.status_code == 200
                    result = response.json()
                    assert result["status"] == "completed"
                    assert result["checked_pipelines"] == 4
                    assert result["updated_pipelines"] == 2

                    async with session_maker() as session:
                        query = select(Pipeline).where(Pipeline.id == pipeline1.id)
                        db_result = await session.execute(query)
                        updated_pipeline = db_result.scalars().first()
                        assert updated_pipeline.status == PipelineStatus.COMMITTED

                        query = select(Pipeline).where(Pipeline.id == pipeline3.id)
                        db_result = await session.execute(query)
                        updated_pipeline3 = db_result.scalars().first()
                        assert updated_pipeline3.commit_job_id == 12349


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

        mock_fm_instance.get_build_info = AsyncMock(
            return_value={"build": {"commit_job_id": 12345}}
        )

        mock_fm_instance.get_job = AsyncMock(return_value={"status": 3})

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


@pytest.mark.asyncio
async def test_check_jobs_endpoint_includes_published_pipelines(
    client, db_session_maker, auth_headers
):
    """Test that check-jobs endpoint includes PUBLISHED pipelines with job IDs"""
    session_maker = db_session_maker

    now = datetime.now()

    # PUBLISHED pipeline with publish_job_id - should be included
    published_pipeline_with_publish = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.PublishedApp1",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=12345,
        flat_manager_repo="stable",
        created_at=now - timedelta(hours=1),
        published_at=now - timedelta(minutes=30),
        params={},
    )

    # PUBLISHED pipeline with update_repo_job_id - should be included
    published_pipeline_with_update = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.PublishedApp2",
        status=PipelineStatus.PUBLISHED,
        update_repo_job_id=67890,
        flat_manager_repo="beta",
        created_at=now - timedelta(hours=2),
        published_at=now - timedelta(minutes=15),
        params={},
    )

    # PUBLISHED pipeline with no job IDs - should NOT be included
    published_pipeline_no_jobs = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.PublishedApp3",
        status=PipelineStatus.PUBLISHED,
        flat_manager_repo="stable",
        created_at=now - timedelta(hours=1),
        published_at=now - timedelta(minutes=45),
        params={},
    )

    # Old PUBLISHED pipeline - should NOT be included (published > 24h ago)
    old_published_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.OldApp",
        status=PipelineStatus.PUBLISHED,
        publish_job_id=11111,
        flat_manager_repo="stable",
        created_at=now - timedelta(days=2),
        published_at=now - timedelta(days=1, hours=1),
        params={},
    )

    async with session_maker() as session:
        session.add_all(
            [
                published_pipeline_with_publish,
                published_pipeline_with_update,
                published_pipeline_no_jobs,
                old_published_pipeline,
            ]
        )
        await session.commit()

    with patch("app.services.job_monitor.FlatManagerClient") as mock_fm_class:
        mock_fm_instance = AsyncMock()
        mock_fm_class.return_value = mock_fm_instance
        mock_fm_instance.get_job = AsyncMock(return_value={"status": 3})  # ENDED

        response = client.post(
            "/api/pipelines/check-jobs",
            headers=auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        # Should include 2 PUBLISHED pipelines (with publish_job_id and update_repo_job_id)
        assert result["checked_pipelines"] == 2
        assert result["updated_pipelines"] == 2
