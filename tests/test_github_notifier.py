import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.services.github_notifier import GitHubNotifier
from app.utils.flat_manager import FlatManagerClient


@pytest.fixture
def github_notifier():
    mock_flat_manager = MagicMock(spec=FlatManagerClient)
    return GitHubNotifier(flat_manager_client=mock_flat_manager)


@pytest.fixture
def mock_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={
            "sha": "abc123def456",
            "repo": "flathub/org.test.App",
            "pr_number": "42",
        },
        triggered_by=PipelineTrigger.WEBHOOK,
        build_id="build-123",
        flat_manager_repo="stable",
        log_url="https://example.com/logs/123",
        created_at=datetime.now(),
    )


@pytest.mark.asyncio
async def test_notify_build_status_success(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(
            mock_pipeline, "success", log_url="https://example.com/custom-log"
        )

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="success",
            git_repo="flathub/org.test.App",
            description="Build succeeded.",
            target_url="https://example.com/custom-log",
        )


@pytest.mark.asyncio
async def test_notify_build_status_failure(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(mock_pipeline, "failure")

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="failure",
            git_repo="flathub/org.test.App",
            description="Build failed.",
            target_url="https://example.com/logs/123",
        )


@pytest.mark.asyncio
async def test_notify_build_status_cancelled(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(mock_pipeline, "cancelled")

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="failure",
            git_repo="flathub/org.test.App",
            description="Build cancelled.",
            target_url="https://example.com/logs/123",
        )


@pytest.mark.asyncio
async def test_notify_build_status_unknown(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(mock_pipeline, "unknown_status")

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="failure",
            git_repo="flathub/org.test.App",
            description="Build status: unknown_status.",
            target_url="https://example.com/logs/123",
        )


@pytest.mark.asyncio
async def test_notify_build_status_missing_params(github_notifier, mock_pipeline):
    mock_pipeline.params = {}

    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(mock_pipeline, "success")

        mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_notify_build_status_no_log_url(github_notifier, mock_pipeline):
    mock_pipeline.log_url = None

    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(mock_pipeline, "success")

        mock_update.assert_called_once()
        assert mock_update.call_args[1]["target_url"] == ""


@pytest.mark.asyncio
async def test_notify_build_started(github_notifier, mock_pipeline):
    log_url = "https://example.com/logs/456"

    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_started(mock_pipeline, log_url)

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="pending",
            git_repo="flathub/org.test.App",
            description="Build in progress",
            target_url=log_url,
        )


@pytest.mark.asyncio
async def test_notify_build_started_missing_params(github_notifier, mock_pipeline):
    mock_pipeline.params = {"sha": "abc123"}

    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_started(mock_pipeline, "http://log")

        mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_notify_pr_build_started(github_notifier, mock_pipeline):
    log_url = "https://example.com/logs/789"

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_started(mock_pipeline, log_url)

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment=f"🚧 Started [test build]({log_url}).",
        )


@pytest.mark.asyncio
async def test_notify_pr_build_started_invalid_pr_number(
    github_notifier, mock_pipeline
):
    mock_pipeline.params["pr_number"] = "not_a_number"

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_started(mock_pipeline, "http://log")

        mock_comment.assert_not_called()


@pytest.mark.asyncio
async def test_notify_pr_build_started_exception(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        mock_comment.side_effect = Exception("API Error")

        await github_notifier.notify_pr_build_started(mock_pipeline, "http://log")


@pytest.mark.asyncio
async def test_notify_pr_build_complete_success_with_download(
    github_notifier, mock_pipeline
):
    github_notifier.flat_manager.get_flatpakref_url.return_value = (
        "https://dl.flathub.org/build-repo/build-123/org.test.App.flatpakref"
    )

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "success")

        expected_comment = (
            "🚧 [Test build succeeded](https://example.com/logs/123). "
            "To test this build, install it from the testing repository:\n\n"
            "```\nflatpak install --user "
            "https://dl.flathub.org/build-repo/build-123/org.test.App.flatpakref\n```"
        )
        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App", pr_number=42, comment=expected_comment
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_success_no_build_id(
    github_notifier, mock_pipeline
):
    mock_pipeline.build_id = None

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "success")

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment="🚧 [Test build succeeded](https://example.com/logs/123).",
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_failure(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "failure")

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment="🚧 [Test build](https://example.com/logs/123) failed.",
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_cancelled(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "cancelled")

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment="🚧 [Test build](https://example.com/logs/123) was cancelled.",
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_missing_params(github_notifier, mock_pipeline):
    mock_pipeline.params = {"sha": "abc123"}

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "success")

        mock_comment.assert_not_called()


@pytest.mark.asyncio
async def test_create_stable_build_failure_issue(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        await github_notifier.create_stable_build_failure_issue(mock_pipeline)

        expected_body = (
            "The stable build pipeline for `org.test.App` failed.\n\n"
            "Commit SHA: `abc123def456`\n"
            "Build log: https://example.com/logs/123\n\n"
            "cc @flathub/build-moderation"
        )

        mock_issue.assert_called_once_with(
            git_repo="flathub/org.test.App",
            title="Stable build failed",
            body=expected_body,
        )


@pytest.mark.asyncio
async def test_create_stable_build_failure_issue_beta_repo(
    github_notifier, mock_pipeline
):
    mock_pipeline.flat_manager_repo = "beta"

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        await github_notifier.create_stable_build_failure_issue(mock_pipeline)

        mock_issue.assert_not_called()


@pytest.mark.asyncio
async def test_create_stable_build_failure_issue_no_log_url(
    github_notifier, mock_pipeline
):
    mock_pipeline.log_url = None

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        await github_notifier.create_stable_build_failure_issue(mock_pipeline)

        expected_body = (
            "The stable build pipeline for `org.test.App` failed.\n\n"
            "Commit SHA: `abc123def456`\n"
            "Build log URL not available.\n\n"
            "cc @flathub/build-moderation"
        )

        mock_issue.assert_called_once()
        assert mock_issue.call_args[1]["body"] == expected_body


@pytest.mark.asyncio
async def test_create_stable_build_failure_issue_exception(
    github_notifier, mock_pipeline
):
    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.side_effect = Exception("API Error")

        await github_notifier.create_stable_build_failure_issue(mock_pipeline)


@pytest.mark.asyncio
async def test_handle_build_completion_success(github_notifier, mock_pipeline):
    with patch.object(github_notifier, "notify_build_status") as mock_status:
        with patch.object(github_notifier, "notify_pr_build_complete") as mock_pr:
            await github_notifier.handle_build_completion(mock_pipeline, "success")

            mock_status.assert_called_once_with(mock_pipeline, "success")
            mock_pr.assert_called_once_with(mock_pipeline, "success")


@pytest.mark.asyncio
async def test_handle_build_completion_failure_stable(github_notifier, mock_pipeline):
    with patch.object(github_notifier, "notify_build_status") as mock_status:
        with patch.object(
            github_notifier, "create_stable_build_failure_issue"
        ) as mock_issue:
            with patch.object(github_notifier, "notify_pr_build_complete") as mock_pr:
                await github_notifier.handle_build_completion(mock_pipeline, "failure")

                mock_status.assert_called_once_with(mock_pipeline, "failure")
                mock_issue.assert_called_once_with(mock_pipeline)
                mock_pr.assert_called_once_with(mock_pipeline, "failure")


@pytest.mark.asyncio
async def test_handle_build_completion_no_pr(github_notifier, mock_pipeline):
    mock_pipeline.params = {"sha": "abc123", "repo": "flathub/test"}

    with patch.object(github_notifier, "notify_build_status") as mock_status:
        with patch.object(github_notifier, "notify_pr_build_complete") as mock_pr:
            await github_notifier.handle_build_completion(mock_pipeline, "success")

            mock_status.assert_called_once_with(mock_pipeline, "success")
            mock_pr.assert_not_called()


@pytest.mark.asyncio
async def test_handle_build_completion_with_flat_manager(
    github_notifier, mock_pipeline
):
    new_flat_manager = MagicMock(spec=FlatManagerClient)

    with patch.object(github_notifier, "notify_build_status") as mock_status:
        await github_notifier.handle_build_completion(
            mock_pipeline, "success", flat_manager_client=new_flat_manager
        )

        assert github_notifier.flat_manager == new_flat_manager
        mock_status.assert_called_once()


@pytest.mark.asyncio
async def test_handle_build_started(github_notifier, mock_pipeline):
    log_url = "https://example.com/new-log"

    with patch.object(github_notifier, "notify_build_started") as mock_started:
        with patch.object(github_notifier, "notify_pr_build_started") as mock_pr:
            await github_notifier.handle_build_started(mock_pipeline, log_url)

            mock_started.assert_called_once_with(mock_pipeline, log_url)
            mock_pr.assert_called_once_with(mock_pipeline, log_url)


@pytest.mark.asyncio
async def test_handle_build_started_no_pr(github_notifier, mock_pipeline):
    mock_pipeline.params = {"sha": "abc123", "repo": "flathub/test"}
    log_url = "https://example.com/new-log"

    with patch.object(github_notifier, "notify_build_started") as mock_started:
        with patch.object(github_notifier, "notify_pr_build_started") as mock_pr:
            await github_notifier.handle_build_started(mock_pipeline, log_url)

            mock_started.assert_called_once_with(mock_pipeline, log_url)
            mock_pr.assert_not_called()
