import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from app.models import Pipeline, PipelineStatus, PipelineTrigger, ReprocheckIssue
from app.services.reprocheck_notification import ReprocheckNotificationService


@pytest.fixture
def notification_service():
    return ReprocheckNotificationService()


@pytest.fixture
def mock_build_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={
            "sha": "abc123def456",
            "repo": "flathub/org.test.App",
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        flat_manager_repo="stable",
        created_at=datetime.now(),
    )


@pytest.fixture
def mock_reprocheck_pipeline(mock_build_pipeline):
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={
            "build_pipeline_id": str(mock_build_pipeline.id),
            "reprocheck_result": {
                "status_code": "42",
                "result_url": "https://buildbot.flathub.org/builds/12345",
            },
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=datetime.now(),
    )


@pytest.mark.asyncio
async def test_handle_failure_creates_new_issue(
    notification_service,
    mock_reprocheck_pipeline,
    mock_build_pipeline,
    db_session_maker,
):
    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(mock_reprocheck_pipeline)
        await db.flush()

        with patch(
            "app.services.reprocheck_notification.create_github_issue"
        ) as mock_create:
            mock_create.return_value = (
                "https://github.com/flathub/org.test.App/issues/1",
                1,
            )

            await notification_service.handle_reprocheck_result(
                db, mock_reprocheck_pipeline
            )

            mock_create.assert_called_once()
            call_args = mock_create.call_args
            assert call_args[0][0] == "flathub/org.test.App"
            assert call_args[0][1] == "Reproducible build check failed"
            assert "abc123def456" in call_args[0][2]

            from sqlalchemy import select

            result = await db.execute(
                select(ReprocheckIssue).where(ReprocheckIssue.app_id == "org.test.App")
            )
            issue = result.scalar_one_or_none()
            assert issue is not None
            assert issue.issue_number == 1


@pytest.mark.asyncio
async def test_handle_failure_adds_comment_to_open_issue(
    notification_service,
    mock_reprocheck_pipeline,
    mock_build_pipeline,
    db_session_maker,
):
    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(mock_reprocheck_pipeline)

        existing_issue = ReprocheckIssue(app_id="org.test.App", issue_number=123)
        db.add(existing_issue)
        await db.flush()

        with (
            patch(
                "app.services.reprocheck_notification.get_github_issue"
            ) as mock_get_issue,
            patch(
                "app.services.reprocheck_notification.add_issue_comment"
            ) as mock_add_comment,
        ):
            mock_get_issue.return_value = {"state": "open", "state_reason": None}
            mock_add_comment.return_value = True

            await notification_service.handle_reprocheck_result(
                db, mock_reprocheck_pipeline
            )

            mock_get_issue.assert_called_once_with("flathub/org.test.App", 123)
            mock_add_comment.assert_called_once()
            assert "Another failure" in mock_add_comment.call_args[0][2]


@pytest.mark.asyncio
async def test_handle_failure_reopens_manually_closed_issue(
    notification_service,
    mock_reprocheck_pipeline,
    mock_build_pipeline,
    db_session_maker,
):
    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(mock_reprocheck_pipeline)

        existing_issue = ReprocheckIssue(app_id="org.test.App", issue_number=123)
        db.add(existing_issue)
        await db.flush()

        with (
            patch(
                "app.services.reprocheck_notification.get_github_issue"
            ) as mock_get_issue,
            patch(
                "app.services.reprocheck_notification.reopen_github_issue"
            ) as mock_reopen,
            patch(
                "app.services.reprocheck_notification.add_issue_comment"
            ) as mock_add_comment,
        ):
            mock_get_issue.return_value = {
                "state": "closed",
                "state_reason": "not_planned",
            }
            mock_reopen.return_value = True
            mock_add_comment.return_value = True

            await notification_service.handle_reprocheck_result(
                db, mock_reprocheck_pipeline
            )

            mock_reopen.assert_called_once_with("flathub/org.test.App", 123)
            mock_add_comment.assert_called_once()
            assert "Reopening" in mock_add_comment.call_args[0][2]


@pytest.mark.asyncio
async def test_handle_failure_creates_new_issue_after_auto_close(
    notification_service,
    mock_reprocheck_pipeline,
    mock_build_pipeline,
    db_session_maker,
):
    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(mock_reprocheck_pipeline)

        existing_issue = ReprocheckIssue(app_id="org.test.App", issue_number=123)
        db.add(existing_issue)
        await db.flush()

        with (
            patch(
                "app.services.reprocheck_notification.get_github_issue"
            ) as mock_get_issue,
            patch(
                "app.services.reprocheck_notification.create_github_issue"
            ) as mock_create,
        ):
            mock_get_issue.return_value = {
                "state": "closed",
                "state_reason": "completed",
            }
            mock_create.return_value = (
                "https://github.com/flathub/org.test.App/issues/2",
                2,
            )

            await notification_service.handle_reprocheck_result(
                db, mock_reprocheck_pipeline
            )

            mock_create.assert_called_once()

            from sqlalchemy import select

            result = await db.execute(
                select(ReprocheckIssue).where(ReprocheckIssue.app_id == "org.test.App")
            )
            issue = result.scalar_one_or_none()
            assert issue is not None
            assert issue.issue_number == 2


@pytest.mark.asyncio
async def test_handle_success_closes_existing_issue(
    notification_service, mock_build_pipeline, db_session_maker
):
    reprocheck_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={
            "build_pipeline_id": str(mock_build_pipeline.id),
            "reprocheck_result": {
                "status_code": "0",
                "result_url": "https://buildbot.flathub.org/builds/12345",
            },
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=datetime.now(),
    )

    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(reprocheck_pipeline)

        existing_issue = ReprocheckIssue(app_id="org.test.App", issue_number=123)
        db.add(existing_issue)
        await db.flush()

        with (
            patch(
                "app.services.reprocheck_notification.add_issue_comment"
            ) as mock_add_comment,
            patch(
                "app.services.reprocheck_notification.close_github_issue"
            ) as mock_close,
        ):
            mock_add_comment.return_value = True
            mock_close.return_value = True

            await notification_service.handle_reprocheck_result(db, reprocheck_pipeline)

            mock_add_comment.assert_called_once()
            assert "reproducible" in mock_add_comment.call_args[0][2]
            mock_close.assert_called_once_with("flathub/org.test.App", 123)

            from sqlalchemy import select

            result = await db.execute(
                select(ReprocheckIssue).where(ReprocheckIssue.app_id == "org.test.App")
            )
            issue = result.scalar_one_or_none()
            assert issue is None


@pytest.mark.asyncio
async def test_handle_success_no_existing_issue(
    notification_service, mock_build_pipeline, db_session_maker
):
    reprocheck_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={
            "build_pipeline_id": str(mock_build_pipeline.id),
            "reprocheck_result": {
                "status_code": "0",
                "result_url": "https://buildbot.flathub.org/builds/12345",
            },
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=datetime.now(),
    )

    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(reprocheck_pipeline)
        await db.flush()

        with (
            patch(
                "app.services.reprocheck_notification.add_issue_comment"
            ) as mock_add_comment,
            patch(
                "app.services.reprocheck_notification.close_github_issue"
            ) as mock_close,
        ):
            await notification_service.handle_reprocheck_result(db, reprocheck_pipeline)

            mock_add_comment.assert_not_called()
            mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_handle_failure_missing_build_pipeline(
    notification_service, db_session_maker
):
    reprocheck_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={
            "build_pipeline_id": str(uuid.uuid4()),
            "reprocheck_result": {
                "status_code": "42",
                "result_url": "https://buildbot.flathub.org/builds/12345",
            },
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=datetime.now(),
    )

    async with db_session_maker() as db:
        db.add(reprocheck_pipeline)
        await db.flush()

        with patch(
            "app.services.reprocheck_notification.create_github_issue"
        ) as mock_create:
            await notification_service.handle_reprocheck_result(db, reprocheck_pipeline)

            mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_handle_failure_missing_status_code(
    notification_service, mock_build_pipeline, db_session_maker
):
    reprocheck_pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={
            "build_pipeline_id": str(mock_build_pipeline.id),
            "reprocheck_result": {},
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=datetime.now(),
    )

    async with db_session_maker() as db:
        db.add(mock_build_pipeline)
        db.add(reprocheck_pipeline)
        await db.flush()

        with patch(
            "app.services.reprocheck_notification.create_github_issue"
        ) as mock_create:
            await notification_service.handle_reprocheck_result(db, reprocheck_pipeline)

            mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_failure_description_not_reproducible(notification_service):
    assert notification_service._get_failure_description("42") == "not reproducible"


@pytest.mark.asyncio
async def test_failure_description_failed_to_build(notification_service):
    assert notification_service._get_failure_description("1") == "failed to build"
    assert notification_service._get_failure_description("127") == "failed to build"


@pytest.mark.asyncio
async def test_is_failure(notification_service):
    assert notification_service._is_failure("0") is False
    assert notification_service._is_failure("42") is True
    assert notification_service._is_failure("1") is True
