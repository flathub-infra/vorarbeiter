import uuid
from unittest.mock import patch
from datetime import datetime

import pytest

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines.build import BuildPipeline


@pytest.fixture
def build_pipeline():
    return BuildPipeline()


@pytest.fixture
def stable_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={},
        flat_manager_repo="stable",
        build_id=123,
        update_repo_job_id=456,
        callback_token="test_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.fixture
def beta_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={},
        flat_manager_repo="beta",
        build_id=123,
        update_repo_job_id=456,
        callback_token="test_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.fixture
def stable_pipeline_with_reprocheck():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={},
        repro_pipeline_id=uuid.uuid4(),
        flat_manager_repo="stable",
        build_id=123,
        update_repo_job_id=456,
        callback_token="test_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.mark.asyncio
async def test_handle_publication_dispatches_reprocheck_for_stable_repo(
    build_pipeline, stable_pipeline
):
    """Test that handle_publication dispatches reprocheck for stable repos without existing reprocheck."""
    with patch.object(build_pipeline, "_dispatch_reprocheck_workflow") as mock_dispatch:
        mock_dispatch.return_value = None

        await build_pipeline.handle_publication(stable_pipeline)

        mock_dispatch.assert_called_once_with(stable_pipeline)


@pytest.mark.asyncio
async def test_handle_publication_skips_non_stable_repo(build_pipeline, beta_pipeline):
    """Test that handle_publication does NOT dispatch reprocheck for non-stable repos."""
    with patch.object(build_pipeline, "_dispatch_reprocheck_workflow") as mock_dispatch:
        await build_pipeline.handle_publication(beta_pipeline)

        mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_handle_publication_skips_existing_reprocheck(
    build_pipeline, stable_pipeline_with_reprocheck
):
    """Test that handle_publication does NOT dispatch reprocheck if reprocheck_pipeline_id already exists."""
    with patch.object(build_pipeline, "_dispatch_reprocheck_workflow") as mock_dispatch:
        await build_pipeline.handle_publication(stable_pipeline_with_reprocheck)

        mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_reprocheck_workflow_creates_pipeline_and_dispatches(
    build_pipeline, stable_pipeline
):
    """Test that _dispatch_reprocheck_workflow creates pipeline and dispatches workflow."""
    reprocheck_pipeline_id = uuid.uuid4()
    mock_reprocheck_pipeline = Pipeline(
        id=reprocheck_pipeline_id,
        app_id=stable_pipeline.app_id,
        callback_token="reprocheck_token",
        params={"workflow_id": "reprocheck.yml"},
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    with (
        patch.object(build_pipeline, "create_pipeline") as mock_create,
        patch.object(build_pipeline, "start_pipeline") as mock_start,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.base_url = "https://test.example.com"
        mock_create.return_value = mock_reprocheck_pipeline
        mock_start.return_value = mock_reprocheck_pipeline

        await build_pipeline._dispatch_reprocheck_workflow(stable_pipeline)

        mock_create.assert_called_once_with(
            app_id=stable_pipeline.app_id,
            params={
                "build_pipeline_id": str(stable_pipeline.id),
                "owner": "flathub-infra",
                "repo": "vorarbeiter",
                "workflow_id": "reprocheck.yml",
                "ref": "main",
            },
        )

        mock_start.assert_called_once_with(reprocheck_pipeline_id)


@pytest.mark.asyncio
async def test_dispatch_reprocheck_workflow_handles_errors_gracefully(
    build_pipeline, stable_pipeline
):
    """Test that _dispatch_reprocheck_workflow handles errors gracefully and logs them."""
    with (
        patch.object(build_pipeline, "create_pipeline") as mock_create,
        patch("app.pipelines.build.logger") as mock_logger,
    ):
        mock_create.side_effect = Exception("Test error")

        # Should not raise exception
        await build_pipeline._dispatch_reprocheck_workflow(stable_pipeline)

        # Should log error
        mock_logger.error.assert_called_once()
        error_call = mock_logger.error.call_args
        assert "Failed to dispatch reprocheck workflow" in str(error_call)
        assert str(stable_pipeline.id) in str(error_call)
        assert str(stable_pipeline.update_repo_job_id) in str(error_call)
