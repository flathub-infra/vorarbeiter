import uuid
from unittest.mock import AsyncMock, patch
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines.build import BuildPipeline, CallbackData
from tests.conftest import create_mock_get_db


@pytest.fixture
def mock_db():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def build_pipeline():
    return BuildPipeline()


@pytest.fixture
def reprocheck_pipeline():
    """Pipeline for reprocheck workflow."""
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={
            "workflow_id": "reprocheck.yml",
            "original_pipeline_id": str(uuid.uuid4()),
        },
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
        callback_token="test_token",
    )


@pytest.fixture
def build_pipeline_obj():
    """Pipeline for regular build workflow."""
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={
            "workflow_id": "build.yml",
        },
        build_id=123,
        flat_manager_repo="stable",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
        callback_token="test_token",
    )


@pytest.mark.asyncio
async def test_reprocheck_callback_success_skips_flat_manager_operations(
    build_pipeline, reprocheck_pipeline
):
    """Test that reprocheck workflow success callbacks skip flat-manager operations."""
    callback_data = CallbackData(status="success")

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = reprocheck_pipeline
    mock_get_db = create_mock_get_db(mock_db_session)

    with (
        patch("app.pipelines.build.get_db", mock_get_db),
        patch("app.pipelines.build.FlatManagerClient") as mock_flat_manager,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier,
    ):
        pipeline, updates = await build_pipeline.handle_callback(
            reprocheck_pipeline.id, callback_data
        )

        assert pipeline.status == PipelineStatus.SUCCEEDED
        assert pipeline.finished_at is not None
        assert updates["pipeline_status"] == "success"

        mock_flat_manager.assert_not_called()
        mock_github_notifier.assert_not_called()


@pytest.mark.asyncio
async def test_reprocheck_callback_failure_skips_flat_manager_operations(
    build_pipeline, reprocheck_pipeline
):
    """Test that reprocheck workflow failure callbacks skip flat-manager operations."""
    callback_data = CallbackData(status="failure")

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = reprocheck_pipeline
    mock_get_db = create_mock_get_db(mock_db_session)

    with (
        patch("app.pipelines.build.get_db", mock_get_db),
        patch("app.pipelines.build.GitHubActionsService") as mock_github_actions,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier,
    ):
        mock_github_service = AsyncMock()
        mock_github_service.check_run_was_cancelled.return_value = False
        mock_github_actions.return_value = mock_github_service

        pipeline, updates = await build_pipeline.handle_callback(
            reprocheck_pipeline.id, callback_data
        )

        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.finished_at is not None
        assert updates["pipeline_status"] == "failure"

        mock_github_notifier.assert_not_called()


@pytest.mark.asyncio
async def test_build_callback_success_performs_flat_manager_operations(
    build_pipeline, build_pipeline_obj
):
    """Test that build workflow success callbacks perform flat-manager operations."""
    callback_data = CallbackData(status="success")

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = build_pipeline_obj
    mock_get_db = create_mock_get_db(mock_db_session)

    with (
        patch("app.pipelines.build.get_db", mock_get_db),
        patch("app.pipelines.build.FlatManagerClient") as mock_flat_manager_class,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier_class,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.flat_manager_url = "https://test.flatmanager.com"
        mock_settings.flat_manager_token = "test_token"

        mock_flat_manager = AsyncMock()
        mock_flat_manager.commit = AsyncMock(return_value={"job": {"id": 999}})
        mock_flat_manager.get_build_info = AsyncMock(
            return_value={"build": {"commit_job_id": 999}}
        )
        mock_flat_manager_class.return_value = mock_flat_manager

        mock_github_notifier = AsyncMock()
        mock_github_notifier_class.return_value = mock_github_notifier

        pipeline, updates = await build_pipeline.handle_callback(
            build_pipeline_obj.id, callback_data
        )

        assert pipeline.status == PipelineStatus.SUCCEEDED
        assert pipeline.finished_at is not None
        assert updates["pipeline_status"] == "success"

        mock_github_notifier.handle_build_completion.assert_called_once()
        mock_flat_manager.commit.assert_called_once_with(
            123, end_of_life=None, end_of_life_rebase=None
        )


@pytest.mark.asyncio
async def test_build_callback_failure_performs_github_notification(
    build_pipeline, build_pipeline_obj
):
    """Test that build workflow failure callbacks perform GitHub notifications."""
    callback_data = CallbackData(status="failure")

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = build_pipeline_obj
    mock_get_db = create_mock_get_db(mock_db_session)

    with (
        patch("app.pipelines.build.get_db", mock_get_db),
        patch("app.pipelines.build.GitHubActionsService") as mock_github_actions,
        patch("app.pipelines.build.GitHubNotifier") as mock_github_notifier_class,
    ):
        mock_github_service = AsyncMock()
        mock_github_service.check_run_was_cancelled.return_value = False
        mock_github_actions.return_value = mock_github_service

        mock_github_notifier = AsyncMock()
        mock_github_notifier_class.return_value = mock_github_notifier

        pipeline, updates = await build_pipeline.handle_callback(
            build_pipeline_obj.id, callback_data
        )

        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.finished_at is not None
        assert updates["pipeline_status"] == "failure"

        mock_github_notifier.handle_build_completion.assert_called_once_with(
            build_pipeline_obj, "failure", flat_manager_client=None
        )
