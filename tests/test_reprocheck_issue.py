import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.models.reprocheck_issue import ReprocheckIssue, ReprocheckIssueStatus
from app.services.reprocheck_issue import (
    ReprocheckIssueService,
    REPROCHECK_STATUS_REPRODUCIBLE,
    REPROCHECK_STATUS_NOT_REPRODUCIBLE,
    REPROCHECK_STATUS_FAILED,
)


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=AsyncSession)
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def build_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.PUBLISHED,
        params={
            "repo": "flathub/org.test.App",
            "sha": "abc123def456",
        },
        flat_manager_repo="stable",
        build_id=123,
        log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/111",
        callback_token="build_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.fixture
def reprocheck_pipeline(build_pipeline):
    return Pipeline(
        id=uuid.uuid4(),
        app_id=build_pipeline.app_id,
        status=PipelineStatus.SUCCEEDED,
        params={
            "workflow_id": "reprocheck.yml",
            "build_pipeline_id": str(build_pipeline.id),
            "reprocheck_result": {
                "status_code": "42",
                "result_url": "https://example.com/diffoscope",
            },
        },
        log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/222",
        callback_token="reprocheck_token",
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
    )


@pytest.fixture
def existing_open_issue(build_pipeline):
    return ReprocheckIssue(
        id=uuid.uuid4(),
        app_id=build_pipeline.app_id,
        git_repo="flathub/org.test.App",
        issue_number=42,
        issue_url="https://github.com/flathub/org.test.App/issues/42",
        status=ReprocheckIssueStatus.OPEN,
        last_status_code="42",
        last_pipeline_id=uuid.uuid4(),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


@pytest.fixture
def existing_closed_issue(build_pipeline):
    return ReprocheckIssue(
        id=uuid.uuid4(),
        app_id=build_pipeline.app_id,
        git_repo="flathub/org.test.App",
        issue_number=42,
        issue_url="https://github.com/flathub/org.test.App/issues/42",
        status=ReprocheckIssueStatus.CLOSED,
        last_status_code="0",
        last_pipeline_id=uuid.uuid4(),
        created_at=datetime.now(),
        updated_at=datetime.now(),
        closed_at=datetime.now(),
    )


class TestReprocheckIssueService:
    @pytest.mark.asyncio
    async def test_creates_new_issue_for_not_reproducible_build(
        self, mock_db, build_pipeline, reprocheck_pipeline
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.reprocheck_issue.create_github_issue",
            new_callable=AsyncMock,
            return_value="https://github.com/flathub/org.test.App/issues/123",
        ) as mock_create_issue:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            )

            mock_create_issue.assert_called_once()
            call_args = mock_create_issue.call_args
            assert call_args[0][0] == "flathub/org.test.App"
            assert "not reproducible" in call_args[0][1].lower()

            mock_db.add.assert_called_once()
            added_issue = mock_db.add.call_args[0][0]
            assert added_issue.app_id == "org.test.App"
            assert added_issue.issue_number == 123
            assert added_issue.status == ReprocheckIssueStatus.OPEN
            assert added_issue.last_status_code == "42"

    @pytest.mark.asyncio
    async def test_creates_new_issue_for_failed_build(
        self, mock_db, build_pipeline, reprocheck_pipeline
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.reprocheck_issue.create_github_issue",
            new_callable=AsyncMock,
            return_value="https://github.com/flathub/org.test.App/issues/456",
        ) as mock_create_issue:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_FAILED,
            )

            mock_create_issue.assert_called_once()
            call_args = mock_create_issue.call_args
            assert "failed" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_updates_existing_issue_with_new_failure(
        self, mock_db, build_pipeline, reprocheck_pipeline, existing_open_issue
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_open_issue
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.reprocheck_issue.add_issue_comment",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_add_comment:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            )

            mock_add_comment.assert_called_once()
            call_args = mock_add_comment.call_args
            assert call_args[0][0] == "flathub/org.test.App"
            assert call_args[0][1] == 42
            assert "still not reproducible" in call_args[0][2]

            assert existing_open_issue.last_status_code == "42"
            assert existing_open_issue.last_pipeline_id == reprocheck_pipeline.id

    @pytest.mark.asyncio
    async def test_closes_issue_when_build_becomes_reproducible(
        self, mock_db, build_pipeline, reprocheck_pipeline, existing_open_issue
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_open_issue
        mock_db.execute.return_value = mock_result

        with (
            patch(
                "app.services.reprocheck_issue.add_issue_comment",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_add_comment,
            patch(
                "app.services.reprocheck_issue.close_github_issue",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_close_issue,
        ):
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_REPRODUCIBLE,
            )

            mock_add_comment.assert_called_once()
            comment = mock_add_comment.call_args[0][2]
            assert "reproducible" in comment.lower()
            assert "closing" in comment.lower()

            mock_close_issue.assert_called_once_with("flathub/org.test.App", 42)

            assert existing_open_issue.status == ReprocheckIssueStatus.CLOSED
            assert existing_open_issue.last_status_code == "0"
            assert existing_open_issue.closed_at is not None

    @pytest.mark.asyncio
    async def test_does_nothing_when_reproducible_and_no_open_issue(
        self, mock_db, build_pipeline, reprocheck_pipeline
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with (
            patch(
                "app.services.reprocheck_issue.create_github_issue",
                new_callable=AsyncMock,
            ) as mock_create_issue,
            patch(
                "app.services.reprocheck_issue.add_issue_comment",
                new_callable=AsyncMock,
            ) as mock_add_comment,
        ):
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_REPRODUCIBLE,
            )

            mock_create_issue.assert_not_called()
            mock_add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_issue_when_previous_was_closed(
        self, mock_db, build_pipeline, reprocheck_pipeline, existing_closed_issue
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_closed_issue
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.reprocheck_issue.create_github_issue",
            new_callable=AsyncMock,
            return_value="https://github.com/flathub/org.test.App/issues/789",
        ) as mock_create_issue:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            )

            mock_create_issue.assert_called_once()
            body = mock_create_issue.call_args[0][2]
            assert "recurrence" in body.lower()
            assert "#42" in body

            assert existing_closed_issue.issue_number == 789
            assert existing_closed_issue.status == ReprocheckIssueStatus.OPEN
            assert existing_closed_issue.closed_at is None

    @pytest.mark.asyncio
    async def test_skips_duplicate_update_for_same_pipeline(
        self, mock_db, build_pipeline, reprocheck_pipeline, existing_open_issue
    ):
        existing_open_issue.last_pipeline_id = reprocheck_pipeline.id

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_open_issue
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.reprocheck_issue.add_issue_comment",
            new_callable=AsyncMock,
        ) as mock_add_comment:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            )

            mock_add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_git_repo(
        self, mock_db, reprocheck_pipeline
    ):
        build_pipeline_no_repo = Pipeline(
            id=uuid.uuid4(),
            app_id="org.test.App",
            status=PipelineStatus.PUBLISHED,
            params={
                "sha": "abc123def456",
            },
            flat_manager_repo="stable",
            callback_token="token",
            created_at=datetime.now(),
            triggered_by=PipelineTrigger.MANUAL,
            provider_data={},
        )

        with patch(
            "app.services.reprocheck_issue.create_github_issue",
            new_callable=AsyncMock,
        ) as mock_create_issue:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline_no_repo,
                REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            )

            mock_create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_issue_body_includes_relevant_links(
        self, mock_db, build_pipeline, reprocheck_pipeline
    ):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.reprocheck_issue.create_github_issue",
            new_callable=AsyncMock,
            return_value="https://github.com/flathub/org.test.App/issues/123",
        ) as mock_create_issue:
            service = ReprocheckIssueService(mock_db)
            await service.handle_reprocheck_result(
                reprocheck_pipeline,
                build_pipeline,
                REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            )

            body = mock_create_issue.call_args[0][2]
            assert "org.test.App" in body
            assert "abc123def456" in body
            assert build_pipeline.log_url in body
            assert reprocheck_pipeline.log_url in body
            assert "flathub/build-moderation" in body


class TestExtractIssueNumber:
    def test_extracts_number_from_valid_url(self, mock_db):
        service = ReprocheckIssueService(mock_db)
        result = service._extract_issue_number(
            "https://github.com/flathub/org.test.App/issues/123"
        )
        assert result == 123

    def test_handles_trailing_slash(self, mock_db):
        service = ReprocheckIssueService(mock_db)
        result = service._extract_issue_number(
            "https://github.com/flathub/org.test.App/issues/456/"
        )
        assert result == 456

    def test_returns_none_for_invalid_url(self, mock_db):
        service = ReprocheckIssueService(mock_db)
        result = service._extract_issue_number("not-a-url")
        assert result is None

    def test_returns_none_for_non_numeric_ending(self, mock_db):
        service = ReprocheckIssueService(mock_db)
        result = service._extract_issue_number(
            "https://github.com/flathub/org.test.App/issues/abc"
        )
        assert result is None
