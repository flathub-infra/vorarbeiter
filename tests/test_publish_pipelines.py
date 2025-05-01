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
        build_id="123",
        started_at=now,
    )
    older_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="122",
        started_at=now - timedelta(hours=1),
    )
    Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.BetaApp",
        flat_manager_repo="beta",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="124",
        started_at=now,
    )
    Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.TestApp",
        flat_manager_repo="test",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="125",
        started_at=now,
    )
    Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.FailedApp",
        flat_manager_repo="stable",
        status=PipelineStatus.FAILED,
        params={"key": "value"},
        build_id="126",
        started_at=now,
    )

    # Another app's pipeline
    other_app_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.Other",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="789",
        started_at=now,
    )

    async with session_maker() as session:
        session.add_all([newer_pipeline, older_pipeline, other_app_pipeline])
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.publish = AsyncMock()
        mock_client.get_build_info = AsyncMock(
            return_value={"build": {"repo_state": 2, "published_state": 0}}
        )
        mock_client.purge = AsyncMock()

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
        build_id="error",
        started_at=datetime.now(),
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.get_build_info = AsyncMock(
            return_value={"build": {"repo_state": 2, "published_state": 0}}
        )
        mock_client.purge = AsyncMock()

        mock_response = Response(
            status_code=400,
            content=b'{"error-type": "some-error", "message": "Publish failed"}',
        )

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
        assert "Publish failed: HTTP 400" in result.errors[0]["error"]
        assert "Publish failed" in result.errors[0]["error"]

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
    # Create a test pipeline without a build_id
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.NoBuildId",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id=None,
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
        assert "No build_id available" in result.errors[0]["error"]

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline is not None
            assert updated_pipeline.status == PipelineStatus.SUCCEEDED
            assert updated_pipeline.published_at is None


@pytest.mark.asyncio
async def test_publish_pipelines_already_published(db_session_maker, client):
    """Test that pipelines are correctly marked published if Flat-Manager says so."""
    session_maker = db_session_maker
    now = datetime.now()
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.AlreadyPublished",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="already_done",
        started_at=now,
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        # Mock get_build_info to return "Published" state
        mock_client.get_build_info = AsyncMock(
            return_value={"build": {"repo_state": 2, "published_state": 2}}
        )
        mock_client.publish = AsyncMock()  # Publish should NOT be called
        mock_client.purge = AsyncMock()

        @asynccontextmanager
        async def override_get_db_for_test():
            async with session_maker() as session_override:
                yield session_override

        app.dependency_overrides[get_db] = override_get_db_for_test

        try:
            result = await publish_pipelines(token="test_token")
        finally:
            app.dependency_overrides.pop(get_db, None)

        # Verify FlatManagerClient.publish was NOT called
        assert mock_client.publish.call_count == 0

        # Verify the pipeline was marked as published
        assert len(result.published) == 1
        assert str(pipeline.id) in result.published
        assert len(result.superseded) == 0
        assert len(result.errors) == 0

        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline is not None
            assert updated_pipeline.status == PipelineStatus.PUBLISHED
            assert updated_pipeline.published_at is not None


@pytest.mark.asyncio
async def test_publish_pipelines_still_processing(db_session_maker, client):
    """Test that pipelines are skipped if Flat-Manager state is Uploading/Validating."""
    session_maker = db_session_maker
    now = datetime.now()
    pipeline_validating = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.Validating",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="validating",
        started_at=now,
    )
    pipeline_uploading = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.Uploading",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="uploading",
        started_at=now,
    )

    async with session_maker() as session:
        session.add_all([pipeline_validating, pipeline_uploading])
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        def get_build_info_side_effect(build_id):
            if build_id == "validating":
                # RepoState::Validating (6), PublishedState::Unpublished (0)
                return {"build": {"repo_state": 6, "published_state": 0}}
            elif build_id == "uploading":
                # RepoState::Uploading (0), PublishedState::Unpublished (0)
                return {"build": {"repo_state": 0, "published_state": 0}}
            else:
                # Default for any other unexpected calls
                return {"build": {"repo_state": 2, "published_state": 0}}

        mock_client.get_build_info = AsyncMock(side_effect=get_build_info_side_effect)
        mock_client.publish = AsyncMock()  # Publish should NOT be called
        mock_client.purge = AsyncMock()

        @asynccontextmanager
        async def override_get_db_for_test():
            async with session_maker() as session_override:
                yield session_override

        app.dependency_overrides[get_db] = override_get_db_for_test

        try:
            result = await publish_pipelines(token="test_token")
        finally:
            app.dependency_overrides.pop(get_db, None)

        # Verify FlatManagerClient.publish was NOT called
        assert mock_client.publish.call_count == 0

        # Verify neither pipeline was marked as published or errored (just skipped)
        assert len(result.published) == 0
        assert len(result.superseded) == 0
        assert len(result.errors) == 0

        # Verify their status remains SUCCEEDED
        async with session_maker() as session:
            res_validating = await session.get(Pipeline, pipeline_validating.id)
            assert res_validating is not None
            assert res_validating.status == PipelineStatus.SUCCEEDED
            assert res_validating.published_at is None

            res_uploading = await session.get(Pipeline, pipeline_uploading.id)
            assert res_uploading is not None
            assert res_uploading.status == PipelineStatus.SUCCEEDED
            assert res_uploading.published_at is None


@pytest.mark.asyncio
async def test_publish_pipelines_failed_validation(db_session_maker, client):
    """Test that pipelines are marked FAILED if Flat-Manager repo_state is 3."""
    session_maker = db_session_maker
    now = datetime.now()
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.FailedValidation",
        flat_manager_repo="stable",
        status=PipelineStatus.SUCCEEDED,
        params={"key": "value"},
        build_id="failed_repo",
        started_at=now,
    )

    async with session_maker() as session:
        session.add(pipeline)
        await session.commit()

    with patch("app.routes.pipelines.FlatManagerClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        # Mock get_build_info to return "Failed" repo_state
        mock_client.get_build_info = AsyncMock(
            return_value={"build": {"repo_state": 3, "published_state": 0}}
        )
        mock_client.publish = AsyncMock()  # Publish should NOT be called
        mock_client.purge = AsyncMock()

        @asynccontextmanager
        async def override_get_db_for_test():
            async with session_maker() as session_override:
                yield session_override

        app.dependency_overrides[get_db] = override_get_db_for_test

        try:
            result = await publish_pipelines(token="test_token")
        finally:
            app.dependency_overrides.pop(get_db, None)

        # Verify FlatManagerClient.publish was NOT called
        assert mock_client.publish.call_count == 0

        # Verify the pipeline was NOT marked published, but recorded an error
        assert len(result.published) == 0
        assert len(result.superseded) == 0
        assert len(result.errors) == 1
        assert result.errors[0]["pipeline_id"] == str(pipeline.id)
        assert "repo_state 3" in result.errors[0]["error"]

        # Verify the pipeline status is now FAILED
        async with session_maker() as session:
            query = select(Pipeline).where(Pipeline.id == pipeline.id)
            db_result = await session.execute(query)
            updated_pipeline = db_result.scalars().first()
            assert updated_pipeline is not None
            assert updated_pipeline.status == PipelineStatus.FAILED
            assert updated_pipeline.published_at is None
            assert updated_pipeline.finished_at is not None
