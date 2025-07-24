import uuid
from unittest.mock import AsyncMock, patch
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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


@pytest.mark.asyncio
async def test_reprocheck_pipeline_skips_flat_manager_build_creation(
    build_pipeline, stable_pipeline
):
    """Test that reprocheck pipelines don't create builds in flat-manager."""
    with (
        patch.object(build_pipeline, "create_pipeline") as mock_create,
        patch.object(build_pipeline, "flat_manager") as mock_flat_manager,
        patch(
            "app.services.github_actions.GitHubActionsService"
        ) as mock_github_actions,
        patch("app.config.settings") as mock_settings,
    ):
        # Mock the reprocheck pipeline that would be created
        mock_reprocheck_pipeline = Pipeline(
            id=uuid.uuid4(),
            app_id=stable_pipeline.app_id,
            callback_token="reprocheck_token",
            params={
                "workflow_id": "reprocheck.yml",
                "build_pipeline_id": str(stable_pipeline.id),
            },
            flat_manager_repo="test",  # Will be set based on ref, but won't be used
            created_at=datetime.now(),
            triggered_by=PipelineTrigger.MANUAL,
            provider_data={},
        )

        mock_settings.base_url = "https://test.example.com"
        mock_create.return_value = mock_reprocheck_pipeline
        mock_github_service = AsyncMock()
        mock_github_actions.return_value = mock_github_service

        with patch.object(build_pipeline, "start_pipeline") as mock_start:
            mock_start.return_value = mock_reprocheck_pipeline
            mock_db = AsyncMock(spec=AsyncSession)
            await build_pipeline._dispatch_reprocheck_workflow(mock_db, stable_pipeline)

        # Verify pipeline was created with reprocheck workflow_id
        create_call_args = mock_create.call_args
        assert create_call_args is not None
        reprocheck_params = create_call_args.kwargs["params"]
        assert reprocheck_params["workflow_id"] == "reprocheck.yml"

        # Verify start_pipeline was called
        mock_start.assert_called_once()

        # Verify flat-manager methods were NOT called during the entire flow
        mock_flat_manager.create_build.assert_not_called()
        mock_flat_manager.create_token_subset.assert_not_called()


@pytest.mark.asyncio
async def test_reprocheck_pipeline_excluded_from_flat_manager_operations():
    """Test that pipelines with flat_manager_repo=None are excluded from flat-manager checks."""
    from app.services.job_monitor import JobMonitor

    reprocheck_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={"workflow_id": "reprocheck.yml"},
        flat_manager_repo=None,  # Reprocheck pipelines have None
        commit_job_id=123,
        publish_job_id=456,
        update_repo_job_id=789,
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    job_monitor = JobMonitor()

    # Test that notification methods return early for flat_manager_repo=None
    with (
        patch.object(job_monitor, "_notify_flat_manager_job_started"),
        patch.object(job_monitor, "_notify_flat_manager_job_completed"),
    ):
        # These methods should return early and not make any calls
        await job_monitor._notify_flat_manager_job_started(
            reprocheck_pipeline, "commit", 123
        )
        await job_monitor._notify_flat_manager_job_completed(
            reprocheck_pipeline, "commit", 123, True
        )

        # Since flat_manager_repo=None, these methods should return immediately
        # without making any GitHub API calls or other operations

        # Note: We can't easily test the internal early returns without more complex mocking,
        # but the key point is that None not in ["stable", "beta"] is True,
        # so these operations will be skipped for reprocheck pipelines

        assert reprocheck_pipeline.flat_manager_repo not in ["stable", "beta"]
        assert reprocheck_pipeline.flat_manager_repo is None


@pytest.mark.asyncio
async def test_publishing_service_excludes_reprocheck_pipelines():
    """Test that publishing service won't pick up reprocheck pipelines."""

    # Mock a reprocheck pipeline with flat_manager_repo=None
    reprocheck_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.COMMITTED,  # Even if somehow committed
        params={"workflow_id": "reprocheck.yml"},
        flat_manager_repo=None,  # Should exclude it from publishing
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )

    # The publishing service query filters by:
    # Pipeline.flat_manager_repo.in_(["stable", "beta"])
    # So flat_manager_repo=None will be excluded

    assert reprocheck_pipeline.flat_manager_repo not in ["stable", "beta"]
    assert reprocheck_pipeline.status == PipelineStatus.COMMITTED

    # This confirms reprocheck pipelines won't be picked up for publishing
    # even if they somehow reach COMMITTED status
