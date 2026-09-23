import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.schemas.pipelines import PipelineTriggerRequest, PipelineType, ReprocheckStatus
from app.services.pipeline import PipelineService


@pytest.fixture
def pipeline_service():
    return PipelineService()


@pytest.fixture
def mock_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"branch": "main"},
        triggered_by=PipelineTrigger.MANUAL,
        build_id=123,
        flat_manager_repo="stable",
        created_at=datetime.now(UTC),
        repro_pipeline_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_list_pipelines_with_filters_basic(pipeline_service, db_session_maker):
    now = datetime.now(UTC)
    records = [
        Pipeline(
            app_id="org.test.App",
            status=PipelineStatus.RUNNING,
            params={"workflow_id": "build.yml"},
            created_at=now,
            started_at=now,
            flat_manager_repo="stable",
        ),
        Pipeline(
            app_id="org.test.App",
            status=PipelineStatus.PENDING,
            params={},
            created_at=now - timedelta(seconds=1),
        ),
        Pipeline(
            app_id="org.test.App",
            status=PipelineStatus.FAILED,
            params={"workflow_id": None},
            created_at=now - timedelta(seconds=2),
        ),
        Pipeline(
            app_id="org.test.App",
            status=PipelineStatus.SUCCEEDED,
            params={
                "workflow_id": "reprocheck.yml",
                "reprocheck_result": {"status_code": "0"},
            },
            created_at=now - timedelta(seconds=3),
        ),
        Pipeline(
            app_id="org.test.AppExtra",
            status=PipelineStatus.PUBLISHED,
            params={},
            created_at=now - timedelta(seconds=4),
        ),
        Pipeline(
            app_id="org.test.100%_App",
            status=PipelineStatus.PUBLISHED,
            params={},
            created_at=now - timedelta(seconds=5),
        ),
    ]
    async with db_session_maker() as db:
        db.add_all(records)
        await db.commit()
        builds = await pipeline_service.list_pipelines_with_filters(
            db, app_id="org.test.App", app_id_match="exact", limit=100
        )
        repro = await pipeline_service.list_pipelines_with_filters(
            db, pipeline_type=PipelineType.REPROCHECK
        )
        assert [p.id for p in builds] == [records[i].id for i in (0, 1, 2)]
        assert [p.id for p in repro] == [records[3].id]
        assert [
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db,
                pipeline_type=PipelineType.REPROCHECK,
                reprocheck_status=ReprocheckStatus.REPRODUCIBLE,
            )
        ] == [records[3].id]
        assert [
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db, app_id="TEST.app", app_id_match="contains"
            )
        ] == [records[0].id, records[1].id, records[2].id, records[4].id]
        assert [
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db, app_id="%_", app_id_match="contains"
            )
        ] == [records[5].id]
        assert [
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db, date_from=now, date_to=now
            )
        ] == [records[0].id]
        assert [
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db, limit=2, offset=2
            )
        ] == [records[2].id, records[4].id]


@pytest.mark.asyncio
async def test_list_pipelines_with_filters_groups_and_limit(
    pipeline_service, db_session_maker
):
    now = datetime.now(UTC)
    active = Pipeline(
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={},
        created_at=now - timedelta(days=2),
    )
    pending = Pipeline(
        app_id="org.test.App",
        status=PipelineStatus.PENDING,
        params={},
        created_at=now - timedelta(days=3),
    )
    completed = [
        Pipeline(
            app_id="org.test.App",
            status=PipelineStatus.PUBLISHED,
            params={},
            created_at=now + timedelta(seconds=i),
        )
        for i in range(120)
    ]
    committed_stable = Pipeline(
        app_id="org.test.Groups",
        status=PipelineStatus.COMMITTED,
        flat_manager_repo="stable",
        params={},
        created_at=now,
    )
    committed_test = Pipeline(
        app_id="org.test.Groups",
        status=PipelineStatus.COMMITTED,
        flat_manager_repo="test",
        params={},
        created_at=now,
    )
    committed_without_repo = Pipeline(
        app_id="org.test.Groups",
        status=PipelineStatus.COMMITTED,
        params={},
        created_at=now,
    )
    superseded = Pipeline(
        app_id="org.test.Groups",
        status=PipelineStatus.SUPERSEDED,
        params={},
        created_at=now,
    )
    async with db_session_maker() as db:
        db.add_all(
            [
                active,
                pending,
                committed_stable,
                committed_test,
                committed_without_repo,
                superseded,
                *completed,
            ]
        )
        await db.commit()
        assert {
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db, group="in-progress"
            )
        } == {active.id, pending.id}
        assert {
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db,
                app_id="org.test.Groups",
                app_id_match="exact",
                group="awaiting-publishing",
            )
        } == {committed_stable.id}
        assert {
            p.id
            for p in await pipeline_service.list_pipelines_with_filters(
                db, app_id="org.test.Groups", app_id_match="exact", group="completed"
            )
        } == {committed_test.id, committed_without_repo.id, superseded.id}
        assert (
            len(
                await pipeline_service.list_pipelines_with_filters(
                    db, group="completed", limit=200
                )
            )
            == 100
        )
        assert (
            len(
                await pipeline_service.list_pipelines_with_filters(
                    db, group="completed", limit=200, offset=100
                )
            )
            == 23
        )


def test_summary_exposes_source_and_linked_reprocheck_only(
    pipeline_service, mock_pipeline
):
    mock_pipeline.params = {
        "repo": "flathub/org.test.App",
        "sha": "abc123",
        "pr_number": 42,
        "callback_token": "private",
    }
    mock_pipeline.failure_issue_url = "https://github.com/flathub/builds/issues/1"
    repro = Pipeline(
        params={
            "reprocheck_result": {
                "status_code": "0",
                "result_url": "https://example.org/result",
            }
        }
    )
    summary = pipeline_service.pipeline_to_summary(mock_pipeline, repro)
    assert (summary.source_repo, summary.sha, summary.pr_number) == (
        "flathub/org.test.App",
        "abc123",
        "42",
    )
    assert (summary.reprocheck_status_code, summary.reprocheck_result_url) == (
        "0",
        "https://example.org/result",
    )
    assert summary.failure_issue_url == mock_pipeline.failure_issue_url
    assert "private" not in summary.model_dump_json()
    assert (
        pipeline_service.pipeline_to_summary(mock_pipeline).reprocheck_status_code
        is None
    )
    repro.params = {"reprocheck_result": None}
    assert (
        pipeline_service.pipeline_to_summary(
            mock_pipeline, repro
        ).reprocheck_status_code
        is None
    )


def test_pipeline_to_summary(pipeline_service, mock_pipeline):
    summary = pipeline_service.pipeline_to_summary(mock_pipeline)

    assert summary.id == str(mock_pipeline.id)
    assert summary.app_id == mock_pipeline.app_id
    assert summary.type == PipelineType.BUILD
    assert summary.status == mock_pipeline.status
    assert summary.repo == mock_pipeline.flat_manager_repo
    assert summary.triggered_by == mock_pipeline.triggered_by
    assert summary.build_id == mock_pipeline.build_id
    assert summary.repro_pipeline_id == mock_pipeline.repro_pipeline_id


def test_pipeline_to_summary_no_repo(pipeline_service, mock_pipeline):
    mock_pipeline.flat_manager_repo = None

    summary = pipeline_service.pipeline_to_summary(mock_pipeline)

    assert summary.repo is None


def test_pipeline_to_response(pipeline_service, mock_pipeline):
    mock_pipeline.log_url = "http://example.com/log"

    response = pipeline_service.pipeline_to_response(mock_pipeline)

    assert response.id == str(mock_pipeline.id)
    assert response.app_id == mock_pipeline.app_id
    assert response.status == mock_pipeline.status
    assert response.repo == mock_pipeline.flat_manager_repo
    assert response.params == mock_pipeline.params
    assert response.log_url == mock_pipeline.log_url
    assert response.repro_pipeline_id == mock_pipeline.repro_pipeline_id


def test_validate_status_valid(pipeline_service):
    result = pipeline_service.validate_status("succeeded")
    assert result == PipelineStatus.SUCCEEDED

    result = pipeline_service.validate_status("pending")
    assert result == PipelineStatus.PENDING


def test_validate_status_invalid(pipeline_service):
    with pytest.raises(ValueError) as exc_info:
        pipeline_service.validate_status("invalid_status")

    assert "Invalid status value" in str(exc_info.value)
    assert "invalid_status" in str(exc_info.value)


def test_validate_trigger_filter_valid(pipeline_service):
    result = pipeline_service.validate_trigger_filter("manual")
    assert result == PipelineTrigger.MANUAL

    result = pipeline_service.validate_trigger_filter("webhook")
    assert result == PipelineTrigger.WEBHOOK


def test_validate_trigger_filter_invalid(pipeline_service):
    with pytest.raises(ValueError) as exc_info:
        pipeline_service.validate_trigger_filter("invalid_trigger")

    assert "Invalid triggered_by value" in str(exc_info.value)
    assert "invalid_trigger" in str(exc_info.value)


@pytest.mark.asyncio
async def test_trigger_manual_pipeline_success(pipeline_service):
    app_id = "org.test.App"
    params = {"branch": "main"}
    pipeline_id = uuid.uuid4()

    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id
    mock_pipeline.app_id = app_id
    mock_pipeline.status = PipelineStatus.RUNNING
    mock_pipeline.triggered_by = PipelineTrigger.MANUAL
    mock_pipeline.params = {"ref": "refs/pull/42/head", "build_type": "default"}
    mock_pipeline.flat_manager_repo = "test"

    with patch("app.pipelines.BuildPipeline") as MockBuildPipeline:
        mock_build = AsyncMock()
        mock_build.create_pipeline.return_value = mock_pipeline
        mock_build.prepare_pipeline_for_start.return_value = mock_pipeline
        mock_build.supersede_conflicting_test_pipelines.return_value = None
        mock_build.should_queue_test_build.return_value = False
        mock_build.start_pipeline.return_value = mock_pipeline
        MockBuildPipeline.return_value = mock_build

        with patch("app.database.get_db") as mock_get_db:
            mock_db = AsyncMock()
            mock_db.get.return_value = mock_pipeline
            mock_get_db.return_value.__aenter__.return_value = mock_db

            result = await pipeline_service.trigger_manual_pipeline(app_id, params)

            assert result["status"] == "created"
            assert result["pipeline_id"] == str(pipeline_id)
            assert result["app_id"] == app_id
            assert result["pipeline_status"] == "running"

            mock_build.create_pipeline.assert_called_once_with(
                app_id=app_id, params=params, webhook_event_id=None
            )
            mock_build.prepare_pipeline_for_start.assert_called_once_with(pipeline_id)
            mock_build.supersede_conflicting_test_pipelines.assert_called_once_with(
                pipeline_id
            )
            mock_build.should_queue_test_build.assert_called_once_with(pipeline_id)
            mock_build.start_pipeline.assert_called_once_with(pipeline_id=pipeline_id)


@pytest.mark.asyncio
async def test_trigger_manual_pipeline_queues_spot_test_when_at_capacity(
    pipeline_service,
):
    app_id = "org.test.App"
    params = {"ref": "refs/pull/42/head"}
    pipeline_id = uuid.uuid4()

    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id
    mock_pipeline.app_id = app_id
    mock_pipeline.status = PipelineStatus.PENDING
    mock_pipeline.triggered_by = PipelineTrigger.MANUAL
    mock_pipeline.params = {"ref": "refs/pull/42/head", "build_type": "medium"}
    mock_pipeline.flat_manager_repo = "test"

    with patch("app.pipelines.BuildPipeline") as MockBuildPipeline:
        mock_build = AsyncMock()
        mock_build.create_pipeline.return_value = mock_pipeline
        mock_build.prepare_pipeline_for_start.return_value = mock_pipeline
        mock_build.supersede_conflicting_test_pipelines.return_value = None
        mock_build.should_queue_test_build.return_value = True
        MockBuildPipeline.return_value = mock_build

        with patch("app.database.get_db") as mock_get_db:
            mock_db = AsyncMock()
            mock_db.get.return_value = mock_pipeline
            mock_get_db.return_value.__aenter__.return_value = mock_db

            result = await pipeline_service.trigger_manual_pipeline(app_id, params)

            assert result["status"] == "created"
            assert result["pipeline_id"] == str(pipeline_id)
            assert result["pipeline_status"] == "pending"
            mock_build.start_pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_manual_pipeline_not_found(pipeline_service):
    app_id = "org.test.App"
    params = {"branch": "main"}
    pipeline_id = uuid.uuid4()

    mock_pipeline = MagicMock(spec=Pipeline)
    mock_pipeline.id = pipeline_id

    with patch("app.pipelines.BuildPipeline") as MockBuildPipeline:
        mock_build = AsyncMock()
        mock_build.create_pipeline.return_value = mock_pipeline
        MockBuildPipeline.return_value = mock_build

        with patch("app.database.get_db") as mock_get_db:
            mock_db = AsyncMock()
            mock_db.get.return_value = None
            mock_get_db.return_value.__aenter__.return_value = mock_db

            with pytest.raises(ValueError) as exc_info:
                await pipeline_service.trigger_manual_pipeline(app_id, params)

            assert f"Pipeline {pipeline_id} not found" in str(exc_info.value)


def test_pipeline_to_summary_reprocheck_type(pipeline_service):
    """Test that reprocheck pipelines get correct type."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"workflow_id": "reprocheck.yml"},
        triggered_by=PipelineTrigger.MANUAL,
        flat_manager_repo=None,
        created_at=datetime.now(UTC),
    )

    summary = pipeline_service.pipeline_to_summary(pipeline)

    assert summary.type == PipelineType.REPROCHECK


def test_pipeline_to_summary_build_type_explicit(pipeline_service):
    """Test that build pipelines with explicit workflow_id get correct type."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"workflow_id": "build.yml"},
        triggered_by=PipelineTrigger.MANUAL,
        flat_manager_repo="stable",
        created_at=datetime.now(UTC),
    )

    summary = pipeline_service.pipeline_to_summary(pipeline)

    assert summary.type == PipelineType.BUILD


def test_pipeline_to_summary_build_type_no_workflow_id(pipeline_service):
    """Test that pipelines without workflow_id default to build type."""
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"branch": "main"},
        triggered_by=PipelineTrigger.MANUAL,
        flat_manager_repo="stable",
        created_at=datetime.now(UTC),
    )

    summary = pipeline_service.pipeline_to_summary(pipeline)

    assert summary.type == PipelineType.BUILD


def test_pipeline_trigger_request_normalizes_commit_pair():
    request = PipelineTriggerRequest(
        app_id="org.test.App",
        params={"sha": "A" * 40, "base_sha": "B" * 40},
    )

    assert request.params == {"sha": "a" * 40, "base_sha": "b" * 40}


def test_pipeline_trigger_request_rejects_invalid_baseline():
    with pytest.raises(ValueError, match="base_sha must differ from sha"):
        PipelineTriggerRequest(
            app_id="org.test.App",
            params={"sha": "A" * 40, "base_sha": "A" * 40},
        )
