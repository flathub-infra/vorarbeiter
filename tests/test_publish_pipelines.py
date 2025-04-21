import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager

import pytest
from httpx import HTTPStatusError, Response
from sqlalchemy.future import select

from app.main import app
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.routes.pipelines import publish_pipelines


@pytest.mark.asyncio
async def test_publish_pipelines_success(db_session_maker, client):
    session_maker = db_session_maker
    now = datetime.now()
    newer_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_url="https://example.com/builds/123",
        started_at=now,
    )
    older_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_url="https://example.com/builds/456",
        started_at=now - timedelta(days=1),
    )

    # Another app's pipeline
    other_app_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.Other",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_url="https://example.com/builds/789",
        started_at=now,
    )

    async with session_maker() as session:
        session.add_all([newer_pipeline, older_pipeline, other_app_pipeline])
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.publish = AsyncMock()

        @asynccontextmanager
        async def override_get_db_for_test():
            async with session_maker() as session_override:
                yield session_override

        app.dependency_overrides[get_db] = override_get_db_for_test

        try:
            result = await publish_pipelines(token="test_token")
        finally:
            # Clean up the override
            app.dependency_overrides.pop(get_db, None)

        # Verify FlatManagerClient was called with correct build IDs
        assert mock_client.publish.call_count == 2
        mock_client.publish.assert_any_call("123")
        mock_client.publish.assert_any_call("789")

        assert len(result.published) == 2
        assert len(result.superseded) == 1
        assert len(result.errors) == 0
        assert str(newer_pipeline.id) in result.published
        assert str(other_app_pipeline.id) in result.published
        assert str(older_pipeline.id) in result.superseded

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == newer_pipeline.id)
            db_result = await session.execute(query)
            updated_newer = db_result.scalars().first()
            assert updated_newer is not None
            assert updated_newer.status == PipelineStatus.PUBLISHED
            assert updated_newer.published_at is not None

            query = select(Pipeline).where(Pipeline.id == older_pipeline.id)
            db_result = await session.execute(query)
            updated_older = db_result.scalars().first()
            assert updated_older is not None
            assert updated_older.status == PipelineStatus.SUPERSEDED

            query = select(Pipeline).where(Pipeline.id == other_app_pipeline.id)
            db_result = await session.execute(query)
            updated_other = db_result.scalars().first()
            assert updated_other is not None
            assert updated_other.status == PipelineStatus.PUBLISHED
            assert updated_other.published_at is not None


@pytest.mark.asyncio
async def test_publish_pipelines_error_handling(db_session_maker, client):
    session_maker = db_session_maker
    # Create a test pipeline that will encounter an error during publishing
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.Error",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_url="https://example.com/builds/error",
        started_at=datetime.now(),
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        mock_response = Response(status_code=400, content=b'{"error": "Bad request"}')

        mock_client.publish = AsyncMock(
            side_effect=HTTPStatusError("Error", request=None, response=mock_response)
        )

        @asynccontextmanager
        async def override_get_db_for_test():
            async with session_maker() as session_override:
                yield session_override

        app.dependency_overrides[get_db] = override_get_db_for_test

        try:
            result = await publish_pipelines(token="test_token")
        finally:
            # Clean up the override
            app.dependency_overrides.pop(get_db, None)

        assert len(result.published) == 0
        assert len(result.superseded) == 0
        assert len(result.errors) == 1
        assert str(pipeline.id) in result.errors[0]["pipeline_id"]
        assert "HTTP error: 400" in result.errors[0]["error"]

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline is not None
            assert updated_pipeline.status == PipelineStatus.SUCCEEDED
            assert updated_pipeline.published_at is None


@pytest.mark.asyncio
async def test_publish_pipelines_no_build_url(db_session_maker, client):
    session_maker = db_session_maker
    # Create a test pipeline without a build_url
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.NoBuildUrl",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_url=None,
        started_at=datetime.now(),
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.publish = AsyncMock()

        @asynccontextmanager
        async def override_get_db_for_test():
            async with session_maker() as session_override:
                yield session_override

        app.dependency_overrides[get_db] = override_get_db_for_test

        try:
            result = await publish_pipelines(token="test_token")
        finally:
            app.dependency_overrides.pop(get_db, None)

        # Verify FlatManagerClient was not called
        assert mock_client.publish.call_count == 0

        # Verify the result has correct structure
        assert len(result.published) == 0
        assert len(result.superseded) == 0
        assert len(result.errors) == 1
        assert str(pipeline.id) in result.errors[0]["pipeline_id"]
        assert "No build_url available" in result.errors[0]["error"]

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline is not None
            assert updated_pipeline.status == PipelineStatus.SUCCEEDED
            assert updated_pipeline.published_at is None
