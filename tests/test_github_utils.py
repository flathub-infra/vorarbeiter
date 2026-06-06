from unittest.mock import AsyncMock, MagicMock, patch

import httpxyz as httpx
import pytest

import app.utils.github as github_module
from app.utils.github import (
    add_comment_reaction,
    add_issue_comment,
    close_github_issue,
    create_github_issue,
    create_pr_comment,
    update_commit_status,
    get_linter_warning_messages,
    set_pr_labels,
)


@pytest.fixture
def mock_settings():
    with patch.object(github_module.settings, "flathubbot_token", "test-token"):
        yield github_module.settings


@pytest.mark.asyncio
async def test_update_commit_status_success(mock_settings, mock_httpx):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        await update_commit_status(
            sha="abc123",
            state="success",
            git_repo="flathub/test-app",
            target_url="https://example.com/build/123",
            description="Build succeeded",
        )

    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == "POST"
    assert (
        call_args[0][1]
        == "https://api.github.com/repos/flathub/test-app/statuses/abc123"
    )
    assert call_args[1]["json"]["state"] == "success"
    assert call_args[1]["json"]["context"] == "builds/x86_64"
    assert call_args[1]["json"]["target_url"] == "https://example.com/build/123"
    assert call_args[1]["json"]["description"] == "Build succeeded"
    assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_update_commit_status_custom_context(mock_settings, mock_httpx):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        await update_commit_status(
            sha="abc123",
            state="pending",
            git_repo="flathub/test-app",
            context="builds/aarch64",
        )

    call_json = mock_httpx.request.call_args[1]["json"]
    assert call_json["context"] == "builds/aarch64"


@pytest.mark.asyncio
async def test_update_commit_status_missing_repo(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await update_commit_status(sha="abc123", state="success", git_repo="")

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_missing_sha(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await update_commit_status(sha="", state="success", git_repo="flathub/test-app")

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_null_sha(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await update_commit_status(
            sha="0000000000000000000000000000000000000000",
            state="success",
            git_repo="flathub/test-app",
        )

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_invalid_state(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await update_commit_status(
            sha="abc123", state="invalid_state", git_repo="flathub/test-app"
        )

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_update_commit_status_request_error_retry(mock_settings, mock_httpx):
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_response_success = MagicMock()
        mock_response_success.raise_for_status = MagicMock()

        mock_httpx.set_response(
            "request",
            side_effect=[
                httpx.RequestError("Network error"),
                httpx.RequestError("Network error"),
                mock_response_success,
            ],
        )
        with mock_httpx.patch():
            await update_commit_status(
                sha="abc123", state="success", git_repo="flathub/test-app"
            )

        assert mock_httpx.request.call_count == 3
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0


@pytest.mark.asyncio
async def test_update_commit_status_request_error_max_retries(
    mock_settings, mock_httpx
):
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_httpx.set_response(
            "request", side_effect=httpx.RequestError("Network error")
        )
        with mock_httpx.patch():
            await update_commit_status(
                sha="abc123", state="success", git_repo="flathub/test-app"
            )

        assert mock_httpx.request.call_count == 4
        assert mock_sleep.call_count == 3


@pytest.mark.asyncio
async def test_update_commit_status_http_error(mock_settings, mock_httpx):
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"

    mock_httpx.set_response(
        "request",
        side_effect=httpx.HTTPStatusError(
            "HTTP error", request=MagicMock(), response=mock_response
        ),
    )
    with mock_httpx.patch():
        await update_commit_status(
            sha="abc123", state="success", git_repo="flathub/test-app"
        )

    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_update_commit_status_retry_on_500_error(mock_settings, mock_httpx):
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_response_500 = MagicMock()
        mock_response_500.status_code = 500
        mock_response_500.text = "Internal Server Error"

        mock_response_success = MagicMock()
        mock_response_success.raise_for_status = MagicMock()

        mock_httpx.set_response(
            "request",
            side_effect=[
                httpx.HTTPStatusError(
                    "HTTP error", request=MagicMock(), response=mock_response_500
                ),
                httpx.HTTPStatusError(
                    "HTTP error", request=MagicMock(), response=mock_response_500
                ),
                mock_response_success,
            ],
        )
        with mock_httpx.patch():
            await update_commit_status(
                sha="abc123", state="success", git_repo="flathub/test-app"
            )

        assert mock_httpx.request.call_count == 3
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0


@pytest.mark.asyncio
async def test_update_commit_status_max_retries_exceeded(mock_settings, mock_httpx):
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_response_500 = MagicMock()
        mock_response_500.status_code = 500
        mock_response_500.text = "Internal Server Error"

        mock_httpx.set_response(
            "request",
            side_effect=httpx.HTTPStatusError(
                "HTTP error", request=MagicMock(), response=mock_response_500
            ),
        )
        with mock_httpx.patch():
            await update_commit_status(
                sha="abc123", state="success", git_repo="flathub/test-app"
            )

        assert mock_httpx.request.call_count == 4
        assert mock_sleep.call_count == 3
        assert mock_sleep.call_args_list[0][0][0] == 1
        assert mock_sleep.call_args_list[1][0][0] == 2
        assert mock_sleep.call_args_list[2][0][0] == 2


@pytest.mark.asyncio
async def test_update_commit_status_unexpected_error(mock_settings, mock_httpx):
    mock_httpx.set_response("request", side_effect=Exception("Unexpected error"))
    with mock_httpx.patch():
        await update_commit_status(
            sha="abc123", state="success", git_repo="flathub/test-app"
        )

    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_pr_comment_success(mock_settings, mock_httpx):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Build started!"
        )

    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == "POST"
    assert (
        call_args[0][1]
        == "https://api.github.com/repos/flathub/test-app/issues/42/comments"
    )
    assert call_args[1]["json"]["body"] == "Build started!"
    assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_create_pr_comment_missing_repo(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await create_pr_comment(git_repo="", pr_number=42, comment="Test comment")

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_create_pr_comment_missing_pr_number(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=0, comment="Test comment"
        )

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_create_pr_comment_request_error(mock_settings, mock_httpx):
    mock_httpx.set_response("request", side_effect=httpx.RequestError("Network error"))
    with mock_httpx.patch():
        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Test comment"
        )

    assert mock_httpx.request.call_count == 4


@pytest.mark.asyncio
async def test_create_pr_comment_http_error(mock_settings, mock_httpx):
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"

    mock_httpx.set_response(
        "request",
        side_effect=httpx.HTTPStatusError(
            "HTTP error", request=MagicMock(), response=mock_response
        ),
    )
    with mock_httpx.patch():
        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Test comment"
        )

    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_pr_comment_unexpected_error(mock_settings, mock_httpx):
    mock_httpx.set_response("request", side_effect=Exception("Unexpected error"))
    with mock_httpx.patch():
        await create_pr_comment(
            git_repo="flathub/test-app", pr_number=42, comment="Test comment"
        )

    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_success(mock_settings, mock_httpx):
    mock_httpx.set_response(
        "request",
        json_data={
            "html_url": "https://github.com/flathub/test-app/issues/123",
            "number": 123,
        },
    )
    with mock_httpx.patch():
        result = await create_github_issue(
            git_repo="flathub/test-app",
            title="Build failed",
            body="The build failed with error XYZ",
        )

    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == "POST"
    assert call_args[0][1] == "https://api.github.com/repos/flathub/test-app/issues"
    assert call_args[1]["json"]["title"] == "Build failed"
    assert call_args[1]["json"]["body"] == "The build failed with error XYZ"
    assert call_args[1]["headers"]["Authorization"] == "token test-token"
    assert result == ("https://github.com/flathub/test-app/issues/123", 123)


@pytest.mark.asyncio
async def test_create_github_issue_missing_repo(mock_settings, mock_httpx):
    with mock_httpx.patch():
        await create_github_issue(
            git_repo="", title="Build failed", body="Error details"
        )

    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_create_github_issue_request_error(mock_settings, mock_httpx):
    mock_httpx.set_response("request", side_effect=httpx.RequestError("Network error"))
    with mock_httpx.patch():
        result = await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

    assert result is None
    assert mock_httpx.request.call_count == 4


@pytest.mark.asyncio
async def test_create_github_issue_http_error(mock_settings, mock_httpx):
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.text = "Validation failed"

    mock_httpx.set_response(
        "request",
        side_effect=httpx.HTTPStatusError(
            "HTTP error", request=MagicMock(), response=mock_response
        ),
    )
    with mock_httpx.patch():
        await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_unexpected_error(mock_settings, mock_httpx):
    mock_httpx.set_response("request", side_effect=Exception("Unexpected error"))
    with mock_httpx.patch():
        await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_github_issue_no_issue_number(mock_settings, mock_httpx):
    mock_httpx.set_response(
        "request",
        json_data={"html_url": "https://github.com/flathub/test-app/issues/123"},
    )
    with mock_httpx.patch():
        result = await create_github_issue(
            git_repo="flathub/test-app", title="Build failed", body="Error details"
        )

    mock_httpx.request.assert_called_once()
    assert result is None


@pytest.mark.asyncio
async def test_close_github_issue_success(mock_settings, mock_httpx):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        result = await close_github_issue(git_repo="flathub/test-app", issue_number=123)

    assert result is True
    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == "PATCH"
    assert call_args[0][1] == "https://api.github.com/repos/flathub/test-app/issues/123"
    assert call_args[1]["json"]["state"] == "closed"
    assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_close_github_issue_missing_repo(mock_settings, mock_httpx):
    with mock_httpx.patch():
        result = await close_github_issue(git_repo="", issue_number=123)

    assert result is False
    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_close_github_issue_missing_issue_number(mock_settings, mock_httpx):
    with mock_httpx.patch():
        result = await close_github_issue(git_repo="flathub/test-app", issue_number=0)

    assert result is False
    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_close_github_issue_http_error(mock_settings, mock_httpx):
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"

    mock_httpx.set_response(
        "request",
        side_effect=httpx.HTTPStatusError(
            "HTTP error", request=MagicMock(), response=mock_response
        ),
    )
    with mock_httpx.patch():
        result = await close_github_issue(git_repo="flathub/test-app", issue_number=123)

    assert result is False
    mock_httpx.request.assert_called_once()


@pytest.mark.asyncio
async def test_add_issue_comment_success(mock_settings, mock_httpx):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        result = await add_issue_comment(
            git_repo="flathub/test-app", issue_number=123, comment="Retry triggered"
        )

    assert result is True
    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == "POST"
    assert (
        call_args[0][1]
        == "https://api.github.com/repos/flathub/test-app/issues/123/comments"
    )
    assert call_args[1]["json"]["body"] == "Retry triggered"
    assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_add_issue_comment_missing_repo(mock_settings, mock_httpx):
    with mock_httpx.patch():
        result = await add_issue_comment(
            git_repo="", issue_number=123, comment="Test comment"
        )

    assert result is False
    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_add_issue_comment_missing_issue_number(mock_settings, mock_httpx):
    with mock_httpx.patch():
        result = await add_issue_comment(
            git_repo="flathub/test-app", issue_number=0, comment="Test comment"
        )

    assert result is False
    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_detection(mock_settings):
    from app.utils.github import GitHubAPIClient

    client = GitHubAPIClient("test-token")

    rate_limit_response = MagicMock()
    rate_limit_response.status_code = 403
    rate_limit_response.json.return_value = {
        "message": "API rate limit exceeded for user ID 12345"
    }

    assert client._is_rate_limit_error(rate_limit_response) is True

    normal_403_response = MagicMock()
    normal_403_response.status_code = 403
    normal_403_response.json.return_value = {"message": "Resource not accessible"}

    assert client._is_rate_limit_error(normal_403_response) is False


@pytest.mark.asyncio
async def test_rate_limit_wait_time_from_retry_after(mock_settings):
    from app.utils.github import GitHubAPIClient

    client = GitHubAPIClient("test-token")

    response = MagicMock()
    response.headers = {"Retry-After": "60"}

    wait_time = client._get_rate_limit_wait_time(response)
    assert wait_time == 60.0


@pytest.mark.asyncio
async def test_rate_limit_wait_time_from_reset_header(mock_settings):
    import time
    from app.utils.github import GitHubAPIClient

    client = GitHubAPIClient("test-token")

    future_timestamp = int(time.time()) + 120
    response = MagicMock()
    response.headers = {"X-RateLimit-Reset": str(future_timestamp)}

    wait_time = client._get_rate_limit_wait_time(response)
    assert 119 <= wait_time <= 122


@pytest.mark.asyncio
async def test_rate_limit_fails_fast(mock_settings, mock_httpx):
    rate_limit_response = mock_httpx.set_response(
        "request",
        status_code=403,
        json_data={"message": "API rate limit exceeded"},
    )
    rate_limit_response.headers = {"Retry-After": "60"}

    with mock_httpx.patch():
        result = await create_github_issue(
            git_repo="flathub/test-app", title="Test", body="Body"
        )

    assert result is None
    assert mock_httpx.request.call_count == 1


@pytest.mark.asyncio
async def test_rate_limit_returns_result_with_retry_after(mock_settings, mock_httpx):
    from app.utils.github import get_github_client

    rate_limit_response = mock_httpx.set_response(
        "request",
        status_code=403,
        json_data={"message": "API rate limit exceeded"},
    )
    rate_limit_response.headers = {"Retry-After": "120"}

    with mock_httpx.patch():
        client = get_github_client()
        result = await client.request_with_result(
            "post",
            "https://api.github.com/test",
            json={},
        )

    assert result.response is None
    assert result.should_queue is True
    assert result.error_type == "rate_limit"
    assert result.retry_after == 120.0


@pytest.mark.asyncio
async def test_linter_warning_messages_deduplicates():
    annotations = [
        {"message": "'foo-bar-baz' warning found in linter repo check. Details: foo"},
        {
            "message": "'foo-bar-baz' warning found in linter manifest check. Details: bar"
        },
        {"message": "'baz-foo-moo' warning found in linter manifest check."},
    ]

    with patch(
        "app.utils.github.get_check_run_annotations",
        return_value=annotations,
    ):
        result = await get_linter_warning_messages(run_id=123)

    assert len(result) == 2

    foo_bar_baz_msgs = [m for m in result if "foo-bar-baz" in m]
    assert len(foo_bar_baz_msgs) == 1

    assert any("baz-foo-moo" in m for m in result)


@pytest.mark.asyncio
async def test_add_comment_reaction_success(mock_settings, mock_httpx):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        result = await add_comment_reaction(
            git_repo="flathub/test-app", comment_id=98765
        )

    assert result is True
    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == "POST"
    assert (
        call_args[0][1]
        == "https://api.github.com/repos/flathub/test-app/issues/comments/98765/reactions"
    )
    assert call_args[1]["json"] == {"content": "+1"}
    assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_add_comment_reaction_missing_repo(mock_settings, mock_httpx):
    with mock_httpx.patch():
        result = await add_comment_reaction(git_repo="", comment_id=98765)

    assert result is False
    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_add_comment_reaction_missing_comment_id(mock_settings, mock_httpx):
    with mock_httpx.patch():
        result = await add_comment_reaction(git_repo="flathub/test-app", comment_id=0)

    assert result is False
    mock_httpx.request.assert_not_called()


@pytest.mark.asyncio
async def test_add_comment_reaction_queues_on_rate_limit(
    mock_settings, mock_httpx, db_session_maker
):
    from sqlalchemy import select
    from app.models.github_task import GitHubTask

    rate_limit_response = mock_httpx.set_response(
        "request",
        status_code=403,
        json_data={"message": "API rate limit exceeded"},
    )
    rate_limit_response.headers = {"Retry-After": "60"}

    with mock_httpx.patch():
        async with db_session_maker() as db:
            result = await add_comment_reaction(
                git_repo="flathub/test-app", comment_id=98765, db=db
            )
            await db.commit()

        async with db_session_maker() as db:
            tasks = (await db.execute(select(GitHubTask))).scalars().all()

    assert result is False
    assert len(tasks) == 1
    assert tasks[0].task_type == "comment_reaction"
    assert tasks[0].method == "post"
    assert tasks[0].payload == {"content": "+1"}
    assert "issues/comments/98765/reactions" in tasks[0].url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replace, expected_method",
    [
        (False, "POST"),
        (True, "PUT"),
    ],
)
async def test_set_pr_labels(mock_settings, mock_httpx, replace, expected_method):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        result = await set_pr_labels(
            git_repo="flathub/test-app",
            pr_number=42,
            labels=["runtime-update"],
            replace=replace,
        )

    assert result is True
    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args

    assert call_args[0][0] == expected_method
    assert (
        call_args[0][1]
        == "https://api.github.com/repos/flathub/test-app/issues/42/labels"
    )
    assert call_args[1]["json"] == {"labels": ["runtime-update"]}
    assert call_args[1]["headers"]["Authorization"] == "token test-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        ["runtime-update"],
        ["runtime-update", "needs-review"],
    ],
)
async def test_set_pr_labels_multiple_labels(mock_settings, mock_httpx, labels):
    mock_httpx.set_response("request")
    with mock_httpx.patch():
        result = await set_pr_labels(
            git_repo="flathub/test-app",
            pr_number=42,
            labels=labels,
        )

    assert result is True
    assert mock_httpx.request.call_args[1]["json"] == {"labels": labels}
