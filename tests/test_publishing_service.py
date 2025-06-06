import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus
from app.services.publishing import PublishingService, PublishResult


@pytest.fixture
def publishing_service():
    return PublishingService()


@pytest.fixture
def mock_db():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def mock_pipelines():
    now = datetime.now()
    return [
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App1",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="stable",
            build_id=1,
            started_at=now,
            params={},
        ),
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App1",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="stable",
            build_id=2,
            started_at=now - timedelta(hours=1),
            params={},
        ),
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App2",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="beta",
            build_id=3,
            started_at=now,
            params={},
        ),
    ]


@pytest.mark.asyncio
async def test_publish_pipelines_success(publishing_service, mock_db, mock_pipelines):
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_pipelines
    mock_db.execute.return_value = mock_result

    with patch.object(
        publishing_service.flat_manager, "get_build_info"
    ) as mock_get_info:
        with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
            with patch.object(publishing_service.flat_manager, "purge") as mock_purge:
                mock_get_info.return_value = {
                    "build": {
                        "repo_state": 2,
                        "published_state": 0,
                        "commit_job_id": 123,
                        "publish_job_id": 456,
                    }
                }

                result = await publishing_service.publish_pipelines(mock_db)

                assert len(result.published) == 2
                assert len(result.superseded) == 1
                assert len(result.errors) == 0

                assert mock_publish.call_count == 2
                assert mock_purge.call_count == 1
                mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_publish_pipelines_no_candidates(publishing_service, mock_db):
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    result = await publishing_service.publish_pipelines(mock_db)

    assert len(result.published) == 0
    assert len(result.superseded) == 0
    assert len(result.errors) == 0
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_get_publishable_pipelines(publishing_service, mock_db, mock_pipelines):
    mock_pipelines.append(
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App3",
            status=PipelineStatus.FAILED,
            flat_manager_repo="stable",
            build_id=4,
            params={},
        )
    )
    mock_pipelines.append(
        Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App4",
            status=PipelineStatus.COMMITTED,
            flat_manager_repo="test",
            build_id=5,
            params={},
        )
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_pipelines[:3]
    mock_db.execute.return_value = mock_result

    result = await publishing_service._get_publishable_pipelines(mock_db)

    assert len(result) == 3
    assert all(p.status == PipelineStatus.COMMITTED for p in result)
    assert all(p.flat_manager_repo in ["stable", "beta"] for p in result)


def test_group_pipelines_for_publishing(publishing_service, mock_pipelines):
    groups = publishing_service._group_pipelines_for_publishing(mock_pipelines)

    assert len(groups) == 2
    assert ("org.test.App1", "stable") in groups
    assert ("org.test.App2", "beta") in groups
    assert len(groups[("org.test.App1", "stable")]) == 2
    assert len(groups[("org.test.App2", "beta")]) == 1


def test_group_pipelines_skip_null_repo(publishing_service, mock_pipelines):
    mock_pipelines[0].flat_manager_repo = None

    groups = publishing_service._group_pipelines_for_publishing(mock_pipelines)

    assert len(groups) == 2
    assert ("org.test.App1", "stable") in groups
    assert len(groups[("org.test.App1", "stable")]) == 1


@pytest.mark.asyncio
async def test_handle_superseded_pipelines(publishing_service, mock_pipelines):
    result = PublishResult()
    duplicates = mock_pipelines[1:2]

    with patch.object(publishing_service.flat_manager, "purge") as mock_purge:
        await publishing_service._handle_superseded_pipelines(duplicates, result)

        assert len(result.superseded) == 1
        assert duplicates[0].status == PipelineStatus.SUPERSEDED
        mock_purge.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_handle_superseded_pipelines_purge_error(
    publishing_service, mock_pipelines
):
    result = PublishResult()
    duplicates = mock_pipelines[1:2]

    with patch.object(publishing_service.flat_manager, "purge") as mock_purge:
        mock_purge.side_effect = httpx.HTTPStatusError(
            "Error",
            request=MagicMock(),
            response=MagicMock(status_code=404, text="Not found"),
        )

        await publishing_service._handle_superseded_pipelines(duplicates, result)

        assert len(result.superseded) == 1
        assert duplicates[0].status == PipelineStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_process_candidate_pipeline_no_build_id(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        flat_manager_repo="stable",
        build_id=None,
        params={},
    )
    result = PublishResult()

    await publishing_service._process_candidate_pipeline(
        pipeline, "org.test.App", "stable", result, datetime.now()
    )

    assert len(result.errors) == 1
    assert "No build_id available" in result.errors[0]["error"]


@pytest.mark.asyncio
async def test_get_build_info_success(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), build_id=123, params={})

    with patch.object(publishing_service.flat_manager, "get_build_info") as mock_get:
        mock_get.return_value = {
            "build": {
                "repo_state": 2,
                "published_state": 0,
                "commit_job_id": 123,
                "publish_job_id": 456,
            }
        }

        result = await publishing_service._get_build_info(pipeline)

        assert result["repo_state"] == 2
        assert result["published_state"] == 0
        mock_get.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_get_build_info_missing_state(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), build_id=123, params={})

    with patch.object(publishing_service.flat_manager, "get_build_info") as mock_get:
        mock_get.return_value = {"build": {"repo_state": 2}}

        with pytest.raises(ValueError) as exc_info:
            await publishing_service._get_build_info(pipeline)

        assert "Missing state information" in str(exc_info.value)


def test_update_job_ids(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(), commit_job_id=None, publish_job_id=None, params={}
    )
    build_data = {"commit_job_id": 123, "publish_job_id": 456}

    publishing_service._update_job_ids(pipeline, build_data)

    assert pipeline.commit_job_id == 123
    assert pipeline.publish_job_id == 456


def test_update_job_ids_partial(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(), commit_job_id=789, publish_job_id=None, params={}
    )
    build_data = {"commit_job_id": 123, "publish_job_id": 456}

    publishing_service._update_job_ids(pipeline, build_data)

    assert pipeline.commit_job_id == 789
    assert pipeline.publish_job_id == 456


@pytest.mark.asyncio
async def test_handle_build_state_already_published(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.SUCCEEDED,
        build_id=123,
        params={},
    )
    build_data = {"published_state": 2, "repo_state": 2}
    result = PublishResult()
    now = datetime.now()

    await publishing_service._handle_build_state(pipeline, build_data, result, now)

    assert pipeline.status == PipelineStatus.PUBLISHED
    assert pipeline.published_at == now
    assert len(result.published) == 1
    assert str(pipeline.id) in result.published


@pytest.mark.asyncio
async def test_handle_build_state_failed(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.SUCCEEDED,
        build_id=123,
        params={},
    )
    build_data = {"published_state": 0, "repo_state": 3}
    result = PublishResult()
    now = datetime.now()

    await publishing_service._handle_build_state(pipeline, build_data, result, now)

    assert pipeline.status == PipelineStatus.FAILED
    assert pipeline.finished_at == now
    assert len(result.errors) == 1
    assert "repo_state 3" in result.errors[0]["error"]


@pytest.mark.asyncio
async def test_handle_build_state_uploading(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )
    build_data = {"published_state": 0, "repo_state": 0}
    result = PublishResult()

    await publishing_service._handle_build_state(
        pipeline, build_data, result, datetime.now()
    )

    # Should skip processing since repo_state is 0 (Uploading)
    assert len(result.published) == 0
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_handle_build_state_ready(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )
    build_data = {"published_state": 0, "repo_state": 2}
    result = PublishResult()
    now = datetime.now()

    with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
        await publishing_service._handle_build_state(pipeline, build_data, result, now)

        mock_publish.assert_called_once_with(123)
        assert pipeline.status == PipelineStatus.COMMITTED  # Should remain COMMITTED
        assert len(result.published) == 1


@pytest.mark.asyncio
async def test_handle_build_state_processing(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        status=PipelineStatus.COMMITTED,
        build_id=123,
        params={},
    )

    for repo_state in [0, 1, 6]:
        build_data = {"published_state": 0, "repo_state": repo_state}
        result = PublishResult()

        await publishing_service._handle_build_state(
            pipeline, build_data, result, datetime.now()
        )

        assert len(result.published) == 0
        assert len(result.errors) == 0
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_try_publish_build_error(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        build_id=123,
        status=PipelineStatus.COMMITTED,
        params={},
    )
    result = PublishResult()

    with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
        mock_publish.side_effect = httpx.HTTPStatusError(
            "Error",
            request=MagicMock(),
            response=MagicMock(status_code=400, text="Bad request"),
        )

        await publishing_service._try_publish_build(pipeline, result, datetime.now())

        assert len(result.errors) == 1
        assert "HTTP 400" in result.errors[0]["error"]
        assert pipeline.status == PipelineStatus.COMMITTED


@pytest.mark.asyncio
async def test_try_publish_build_success(publishing_service):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        build_id=123,
        status=PipelineStatus.COMMITTED,
        params={},
    )
    result = PublishResult()
    now = datetime.now()

    with patch.object(publishing_service.flat_manager, "publish") as mock_publish:
        await publishing_service._try_publish_build(pipeline, result, now)

        mock_publish.assert_called_once_with(123)
        assert pipeline.status == PipelineStatus.COMMITTED  # Should remain COMMITTED
        assert len(result.published) == 1
        assert str(pipeline.id) in result.published


def test_handle_build_error_request_error(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = httpx.RequestError("Network error")

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "communication error" in result.errors[0]["error"]


def test_handle_build_error_http_status_error(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = httpx.HTTPStatusError(
        "Error", request=MagicMock(), response=MagicMock(status_code=500)
    )

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "API error: 500" in result.errors[0]["error"]


def test_handle_build_error_value_error(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = ValueError("Invalid response")

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "response error" in result.errors[0]["error"]


def test_handle_build_error_generic(publishing_service):
    pipeline = Pipeline(id=uuid.uuid4(), params={})
    result = PublishResult()
    error = Exception("Something went wrong")

    publishing_service._handle_build_error(pipeline, error, result)

    assert len(result.errors) == 1
    assert "Unexpected error" in result.errors[0]["error"]
