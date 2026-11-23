import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineStatus


@pytest.mark.asyncio
async def test_fetch_missing_ids_skips_publish_for_test_pipelines(
    db_session_maker, run_recovery
):
    session_maker = db_session_maker

    test_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        commit_job_id=None,
        publish_job_id=None,
        build_id=123,
        flat_manager_repo="test",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=11),
        params={},
    )

    async with session_maker() as session:
        session.add(test_pipeline)
        await session.commit()

    mock_fm_instance = AsyncMock()
    mock_fm_instance.get_build_info.return_value = {
        "build": {"commit_job_id": 789, "publish_job_id": 101112}
    }
    mock_fm_instance.get_job.return_value = {"status": 1}

    with patch(
        "app.services.job_monitor.get_flat_manager_client",
        return_value=mock_fm_instance,
    ):
        result = await run_recovery(session_maker)

        assert result["checked_pipelines"] == 1
        assert result["updated_pipelines"] == 1

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == test_pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline.commit_job_id == 789
            assert updated_pipeline.publish_job_id is None
