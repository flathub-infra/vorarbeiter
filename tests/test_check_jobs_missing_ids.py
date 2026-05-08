import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineStatus


@pytest.mark.asyncio
async def test_check_jobs_fetches_missing_ids(
    db_session_maker, run_check_all_active_pipelines
):
    session_maker = db_session_maker

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

    mock_fm_instance = AsyncMock()

    async def get_build_info(build_id):
        if build_id == 123:
            return {"build": {"commit_job_id": 789, "publish_job_id": 101112}}
        return {"build": {"commit_job_id": 456, "publish_job_id": 131415}}

    mock_fm_instance.get_build_info.side_effect = get_build_info
    mock_fm_instance.get_job.return_value = {"status": 1}

    with patch(
        "app.services.job_monitor.get_flat_manager_client",
        return_value=mock_fm_instance,
    ):
        result = await run_check_all_active_pipelines(session_maker)

        assert result["checked_pipelines"] == 2
        assert result["updated_pipelines"] == 2

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
async def test_check_jobs_handles_partial_missing_ids(
    db_session_maker, run_check_all_active_pipelines
):
    session_maker = db_session_maker

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

    mock_fm_instance = AsyncMock()
    mock_fm_instance.get_build_info.return_value = {
        "build": {"commit_job_id": 888, "publish_job_id": 999}
    }

    with patch(
        "app.services.job_monitor.get_flat_manager_client",
        return_value=mock_fm_instance,
    ):
        result = await run_check_all_active_pipelines(session_maker)

        assert result["checked_pipelines"] == 1
        assert result["updated_pipelines"] == 1

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.commit_job_id == 888
            assert updated_pipeline.publish_job_id == 999


@pytest.mark.asyncio
async def test_check_jobs_handles_fetch_error(
    db_session_maker, run_check_all_active_pipelines
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

    mock_fm_instance = AsyncMock()
    mock_fm_instance.get_build_info.side_effect = Exception("API Error")

    with patch(
        "app.services.job_monitor.get_flat_manager_client",
        return_value=mock_fm_instance,
    ):
        result = await run_check_all_active_pipelines(session_maker)

        assert result["checked_pipelines"] == 1
        assert result["updated_pipelines"] == 0

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.commit_job_id is None
