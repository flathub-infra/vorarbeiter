import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.utils.github import (
    update_commit_status,
    create_pr_comment,
    create_github_issue,
)


@pytest.fixture
def mock_settings():
    with patch("app.utils.github.settings") as mock:
        mock.github_status_token = "test-token"
        yield mock


@pytest.mark.asyncio
async def test_update_commit_status_success(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await update_commit_status(
            sha="abc123",
            state="success",
            git_repo="flathub/test-app",
            target_url="https://example.com/build/123",
            description="Build succeeded",
        )

        mock_client_instance.post.assert_called_once()
        call_args = mock_client_instance.post.call_args

        assert (
            call_args[0][0]
            == "https://api.github.com/repos/flathub/test-app/statuses/abc123"
        )
        assert call_args[1]["json"]["state"] == "success"
        assert call_args[1]["json"]["context"] == "builds/x86_64"
        assert call_args[1]["json"]["target_url"] == "https://example.com/build/123"
        assert call_args[1]["json"]["description"] == "Build succeeded"
        assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_update_commit_status_custom_context(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await update_commit_status(
            sha="abc123",
            state="pending",
            git_repo="flathub/test-app",
            context="builds/aarch64",
        )

        call_json = mock_client_instance.post.call_args[1]["json"]
        assert call_json["context"] == "builds/aarch64"


@pytest.mark.asyncio
async def test_update_commit_status_no_token():
    with patch("app.utils.github.settings") as mock_settings:
        mock_settings.github_status_token = ""

        with patch("httpx.AsyncClient") as MockClient:
            await update_commit_status(
                sha="abc123", state="success", git_repo="flathub/test-app"
            )

            MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_missing_repo(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        await update_commit_status(sha="abc123", state="success", git_repo="")

        MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_missing_sha(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        await update_commit_status(sha="", state="success", git_repo="flathub/test-app")

        MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_invalid_state(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        await update_commit_status(
            sha="abc123", state="invalid_state", git_repo="flathub/test-app"
        )

        MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_request_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(
            side_effect=httpx.RequestError("Network error")
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await update_commit_status(
            sha="abc123", state="success", git_repo="flathub/test-app"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_update_commit_status_http_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not found"

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "HTTP error", request=MagicMock(), response=mock_response
            )
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await update_commit_status(
            sha="abc123", state="success", git_repo="flathub/test-app"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_update_commit_status_unexpected_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(side_effect=Exception("Unexpected error"))
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await update_commit_status(
            sha="abc123", state="success", git_repo="flathub/test-app"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_pr_comment_success(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Build started!"
        )

        mock_client_instance.post.assert_called_once()
        call_args = mock_client_instance.post.call_args

        assert (
            call_args[0][0]
            == "https://api.github.com/repos/flathub/test-app/issues/42/comments"
        )
        assert call_args[1]["json"]["body"] == "Build started!"
        assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_create_pr_comment_no_token():
    with patch("app.utils.github.settings") as mock_settings:
        mock_settings.github_status_token = ""

        with patch("httpx.AsyncClient") as MockClient:
            await create_pr_comment(
                git_repo="flathub/test-app", pr_number=42, comment="Test comment"
            )

            MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_create_pr_comment_missing_repo(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        await create_pr_comment(git_repo="", pr_number=42, comment="Test comment")

        MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_create_pr_comment_missing_pr_number(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=None, comment="Test comment"
        )

        MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_create_pr_comment_request_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(
            side_effect=httpx.RequestError("Network error")
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Test comment"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_pr_comment_http_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "HTTP error", request=MagicMock(), response=mock_response
            )
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Test comment"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_pr_comment_unexpected_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(side_effect=Exception("Unexpected error"))
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Test comment"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_success(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "html_url": "https://github.com/flathub/test-app/issues/123"
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_github_issue(
            git_repo="flathub/test-app",
            title="Build failed",
            body="The build failed with error XYZ",
        )

        mock_client_instance.post.assert_called_once()
        call_args = mock_client_instance.post.call_args

        assert call_args[0][0] == "https://api.github.com/repos/flathub/test-app/issues"
        assert call_args[1]["json"]["title"] == "Build failed"
        assert call_args[1]["json"]["body"] == "The build failed with error XYZ"
        assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_create_github_issue_missing_repo(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        await create_github_issue(
            git_repo="", title="Build failed", body="Error details"
        )

        MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_create_github_issue_request_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(
            side_effect=httpx.RequestError("Network error")
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_http_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = "Validation failed"

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "HTTP error", request=MagicMock(), response=mock_response
            )
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_unexpected_error(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(side_effect=Exception("Unexpected error"))
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

        mock_client_instance.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_no_html_url(mock_settings):
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {}

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

        mock_client_instance.post.assert_called_once()
