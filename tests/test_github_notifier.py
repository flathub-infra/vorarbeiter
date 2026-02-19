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
        build_id=123,
        commit_job_id=12345,
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
            description="Build succeeded",
            target_url="https://example.com/custom-log",
        )


@pytest.mark.asyncio
async def test_notify_build_status_success_no_commit_job_id(
    github_notifier, mock_pipeline
):
    mock_pipeline.commit_job_id = None

    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(
            mock_pipeline, "success", log_url="https://example.com/custom-log"
        )

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="success",
            git_repo="flathub/org.test.App",
            description="Build succeeded",
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
            description="Build failed",
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
            description="Build cancelled",
            target_url="https://example.com/logs/123",
        )


@pytest.mark.asyncio
async def test_notify_build_status_committed(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.update_commit_status") as mock_update:
        await github_notifier.notify_build_status(
            mock_pipeline, "committed", log_url="https://example.com/custom-log"
        )

        mock_update.assert_called_once_with(
            sha="abc123def456",
            state="success",
            git_repo="flathub/org.test.App",
            description="Build ready",
            target_url="https://example.com/custom-log",
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
async def test_notify_build_status_no_log_url_no_commit_job(
    github_notifier, mock_pipeline
):
    mock_pipeline.log_url = None
    mock_pipeline.commit_job_id = None

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
        "https://dl.flathub.org/build-repo/123/org.test.App.flatpakref"
    )

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "success")

        mock_comment.assert_not_called()


@pytest.mark.asyncio
async def test_notify_pr_build_complete_success_no_build_id(
    github_notifier, mock_pipeline
):
    mock_pipeline.build_id = None

    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "success")

        mock_comment.assert_not_called()


@pytest.mark.asyncio
async def test_notify_pr_build_complete_failure(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "failure")

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment=(
                "❌ [Test build](https://example.com/logs/123) failed.\n\n"
                "<details><summary>Help</summary>\n\n"
                "- <code>bot, build</code> - Restart the test build\n"
                "- <code>bot, ping admins</code> - Contact Flathub admins\n"
                "</details>"
            ),
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_committed_with_download(
    github_notifier, mock_pipeline
):
    github_notifier.flat_manager.get_flatpakref_url.return_value = (
        "https://dl.flathub.org/build-repo/123/org.test.App.flatpakref"
    )

    fake_linter_warnings = [
        "A fake warning found in linter repo check",
        "A fake warning found in linter manifest check",
    ]

    with (
        patch(
            "app.services.github_notifier.get_linter_warning_messages",
            return_value=fake_linter_warnings,
        ),
        patch(
            "app.services.github_notifier.get_build_job_arches",
            return_value=["x86_64", "aarch64"],
        ),
        patch("app.services.github_notifier.create_pr_comment") as mock_comment,
    ):
        await github_notifier.notify_pr_build_complete(mock_pipeline, "committed")

        expected_comment = (
            "✅ [Test build succeeded](https://example.com/logs/123). "
            "To test this build, install it from the testing repository:\n\n"
            "```\nflatpak install --user "
            "https://dl.flathub.org/build-repo/123/org.test.App.flatpakref\n```"
            "\n\n*Built for aarch64 and x86_64 architectures.*"
            "\n\n⚠️  Linter warnings:\n\n"
            "_Warnings can be promoted to errors in the future. Please try to resolve them._\n\n"
            "- A fake warning found in linter repo check\n"
            "- A fake warning found in linter manifest check"
        )
        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App", pr_number=42, comment=expected_comment
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_committed_no_build_id(
    github_notifier, mock_pipeline
):
    mock_pipeline.build_id = None

    with (
        patch(
            "app.services.github_notifier.get_build_job_arches",
            return_value=["x86_64"],
        ),
        patch("app.services.github_notifier.create_pr_comment") as mock_comment,
    ):
        await github_notifier.notify_pr_build_complete(mock_pipeline, "committed")

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment="✅ [Test build succeeded](https://example.com/logs/123).\n\n*Built for x86_64 architecture.*",
        )


@pytest.mark.asyncio
async def test_notify_pr_build_complete_cancelled(github_notifier, mock_pipeline):
    with patch("app.services.github_notifier.create_pr_comment") as mock_comment:
        await github_notifier.notify_pr_build_complete(mock_pipeline, "cancelled")

        mock_comment.assert_called_once_with(
            git_repo="flathub/org.test.App",
            pr_number=42,
            comment=(
                "❌ [Test build](https://example.com/logs/123) was cancelled.\n\n"
                "<details><summary>Help</summary>\n\n"
                "- <code>bot, build</code> - Restart the test build\n"
                "- <code>bot, ping admins</code> - Contact Flathub admins\n"
                "</details>"
            ),
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
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_build_failure_issue(mock_pipeline)

        expected_body = (
            "The stable build pipeline for `org.test.App` failed.\n\n"
            "Commit SHA: abc123def456\n"
            "Build log: https://example.com/logs/123\n\n"
            "Please check the logs for details. "
            "If the failure was unexpected, you can retry the build "
            "by commenting `bot, retry` in this issue.\n\n"
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
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_build_failure_issue(mock_pipeline)

        expected_body = (
            "The stable build pipeline for `org.test.App` failed.\n\n"
            "Commit SHA: abc123def456\n"
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
            mock_pr.assert_not_called()


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
async def test_handle_build_completion_cancelled_medium_build(
    github_notifier, mock_pipeline
):
    mock_pipeline.params = {"build_type": "medium"}

    with patch.object(github_notifier, "notify_build_status") as mock_status:
        with patch.object(
            github_notifier, "create_stable_build_failure_issue"
        ) as mock_issue:
            await github_notifier.handle_build_completion(mock_pipeline, "cancelled")

            mock_status.assert_called_once_with(mock_pipeline, "cancelled")
            mock_issue.assert_not_called()


@pytest.mark.asyncio
async def test_handle_build_completion_cancelled_large_build(
    github_notifier, mock_pipeline
):
    mock_pipeline.params = {"build_type": "large"}

    with patch.object(github_notifier, "notify_build_status") as mock_status:
        with patch.object(
            github_notifier, "create_stable_build_failure_issue"
        ) as mock_issue:
            await github_notifier.handle_build_completion(mock_pipeline, "cancelled")

            mock_status.assert_called_once_with(mock_pipeline, "cancelled")
            mock_issue.assert_not_called()


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


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_commit(github_notifier, mock_pipeline):
    job_response = {
        "id": 12345,
        "kind": "COMMIT",
        "status": "BROKEN",
        "log": "Error: Could not commit to repository\nflat-manager: commit failed\nBuild artifacts not found",
    }

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        expected_title = "Stable commit job failed for org.test.App"
        expected_body = (
            "The commit job for `org.test.App` failed in the stable repository.\n\n"
            "**Build Information:**\n"
            "- Commit SHA: abc123def456\n"
            "- Build ID: 123\n"
            "- Build log: https://example.com/logs/123\n\n"
            "**Job Details:**\n"
            "- Job ID: 12345\n"
            "- Job status: https://hub.flathub.org/status/12345\n\n"
            "**Error Details:**\n```\n"
            "Error: Could not commit to repository\n"
            "flat-manager: commit failed\n"
            "Build artifacts not found\n```\n\n"
            "cc @flathub/build-moderation\n\n"
            "This issue is being opened for tracking by Flathub admins and may indicate "
            "an infrastructure problem. Please do not close or modify this until "
            "an admin has responded.\n"
        )

        mock_issue.assert_called_once_with(
            git_repo="flathub/org.test.App",
            title=expected_title,
            body=expected_body,
        )


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_publish(github_notifier, mock_pipeline):
    job_response = {
        "id": 54321,
        "kind": "PUBLISH",
        "status": "BROKEN",
        "log": "Error: Publish failed\nRepository access denied",
    }

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "publish", 54321, job_response
        )

        expected_title = "Stable publish job failed for org.test.App"
        expected_body = (
            "The publish job for `org.test.App` failed in the stable repository.\n\n"
            "**Build Information:**\n"
            "- Commit SHA: abc123def456\n"
            "- Build ID: 123\n"
            "- Build log: https://example.com/logs/123\n\n"
            "**Job Details:**\n"
            "- Job ID: 54321\n"
            "- Job status: https://hub.flathub.org/status/54321\n\n"
            "**Error Details:**\n```\n"
            "Error: Publish failed\n"
            "Repository access denied\n```\n\n"
            "cc @flathub/build-moderation\n\n"
            "This issue is being opened for tracking by Flathub admins and may indicate "
            "an infrastructure problem. Please do not close or modify this until "
            "an admin has responded.\n"
        )

        mock_issue.assert_called_once_with(
            git_repo="flathub/org.test.App",
            title=expected_title,
            body=expected_body,
        )


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_update_repo(
    github_notifier, mock_pipeline
):
    job_response = {
        "id": 98765,
        "kind": "UPDATE_REPO",
        "status": "BROKEN",
        "log": "Error: Repository update failed\nDisk space insufficient",
    }

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "update-repo", 98765, job_response
        )

        expected_title = "Stable repository update job failed for org.test.App"
        expected_body = (
            "The update-repo job for `org.test.App` failed in the stable repository.\n\n"
            "**Build Information:**\n"
            "- Commit SHA: abc123def456\n"
            "- Build ID: 123\n"
            "- Build log: https://example.com/logs/123\n\n"
            "**Job Details:**\n"
            "- Job ID: 98765\n"
            "- Job status: https://hub.flathub.org/status/98765\n\n"
            "**Error Details:**\n```\n"
            "Error: Repository update failed\n"
            "Disk space insufficient\n```\n\n"
            "cc @flathub/build-moderation\n\n"
            "This issue is being opened for tracking by Flathub admins and may indicate "
            "an infrastructure problem. Please do not close or modify this until "
            "an admin has responded.\n"
        )

        mock_issue.assert_called_once_with(
            git_repo="flathub/org.test.App",
            title=expected_title,
            body=expected_body,
        )


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_beta_repo(
    github_notifier, mock_pipeline
):
    mock_pipeline.flat_manager_repo = "beta"
    job_response = {"id": 12345, "log": "Error message"}

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        expected_title = "Beta commit job failed for org.test.App"
        mock_issue.assert_called_once()
        assert mock_issue.call_args[1]["title"] == expected_title


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_test_repo_skipped(
    github_notifier, mock_pipeline
):
    mock_pipeline.flat_manager_repo = "test"
    job_response = {"id": 12345, "log": "Error message"}

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        mock_issue.assert_not_called()


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_long_log(github_notifier, mock_pipeline):
    long_log = "\n".join([f"Line {i}: Some error message" for i in range(50)])
    job_response = {"id": 12345, "log": long_log}

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        mock_issue.assert_called_once()
        body = mock_issue.call_args[1]["body"]
        assert "...\n" in body
        assert "Line 25:" in body
        assert "Line 49:" in body
        assert "Line 0:" not in body


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_no_log(github_notifier, mock_pipeline):
    job_response = {"id": 12345}

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        mock_issue.assert_called_once()
        body = mock_issue.call_args[1]["body"]
        assert "**Error Details:**" not in body


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_no_job_response(
    github_notifier, mock_pipeline
):
    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.return_value = (
            "https://github.com/flathub/org.test.App/issues/1",
            1,
        )
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, None
        )

        mock_issue.assert_called_once()
        body = mock_issue.call_args[1]["body"]
        assert "**Error Details:**" not in body


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_missing_git_repo(
    github_notifier, mock_pipeline
):
    mock_pipeline.params = {"sha": "abc123"}
    job_response = {"id": 12345, "log": "Error message"}

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )

        mock_issue.assert_not_called()


@pytest.mark.asyncio
async def test_create_stable_job_failure_issue_exception(
    github_notifier, mock_pipeline
):
    job_response = {"id": 12345, "log": "Error message"}

    with patch("app.services.github_notifier.create_github_issue") as mock_issue:
        mock_issue.side_effect = Exception("API Error")

        await github_notifier.create_stable_job_failure_issue(
            mock_pipeline, "commit", 12345, job_response
        )
