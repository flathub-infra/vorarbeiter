from unittest.mock import AsyncMock, Mock, patch

import httpx2 as httpx
import pytest

from app.utils.github import GitHubAPIResult
from app.utils.github_app import (
    GitHubAppAuthenticationError,
    GitHubAppInstallationAuth,
)


@pytest.mark.asyncio
async def test_missing_scanner_configuration_is_rejected():
    auth = GitHubAppInstallationAuth()

    with (
        patch("app.utils.github_app.settings.inactive_repos_github_app_id", None),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_installation_id",
            None,
        ),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_private_key_file",
            None,
        ),
        pytest.raises(
            GitHubAppAuthenticationError, match="Missing scanner configuration"
        ),
    ):
        await auth.get_client()


@pytest.mark.asyncio
async def test_installation_token_is_cached_until_refresh_margin():
    auth = GitHubAppInstallationAuth()
    token_response = httpx.Response(
        201,
        json={"token": "installation-token", "expires_at": "2999-01-01T00:00:00Z"},
    )
    exchange_client = Mock()
    exchange_client.request_with_result = AsyncMock(
        return_value=GitHubAPIResult(response=token_response)
    )
    scanner_client = Mock()

    with (
        patch.object(auth, "_app_jwt", return_value="app-jwt"),
        patch(
            "app.utils.github_app.GitHubAPIClient",
            side_effect=[exchange_client, scanner_client],
        ) as client_type,
        patch("app.utils.github_app.settings.inactive_repos_github_app_id", 1),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_installation_id", 2
        ),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_private_key_file",
            "/private-key.pem",
        ),
    ):
        first = await auth.get_client()
        second = await auth.get_client()

    assert first is scanner_client
    assert second is scanner_client
    assert client_type.call_count == 2
    exchange_client.request_with_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidated_installation_token_is_exchanged_again():
    auth = GitHubAppInstallationAuth()
    responses = [
        httpx.Response(
            201,
            json={"token": "first-token", "expires_at": "2999-01-01T00:00:00Z"},
        ),
        httpx.Response(
            201,
            json={"token": "second-token", "expires_at": "2999-01-01T00:00:00Z"},
        ),
    ]
    first_exchange = Mock()
    first_exchange.request_with_result = AsyncMock(
        return_value=GitHubAPIResult(response=responses[0])
    )
    second_exchange = Mock()
    second_exchange.request_with_result = AsyncMock(
        return_value=GitHubAPIResult(response=responses[1])
    )
    first_scanner = Mock()
    second_scanner = Mock()

    with (
        patch.object(auth, "_app_jwt", return_value="app-jwt"),
        patch(
            "app.utils.github_app.GitHubAPIClient",
            side_effect=[
                first_exchange,
                first_scanner,
                second_exchange,
                second_scanner,
            ],
        ),
        patch("app.utils.github_app.settings.inactive_repos_github_app_id", 1),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_installation_id", 2
        ),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_private_key_file",
            "/private-key.pem",
        ),
    ):
        assert await auth.get_client() is first_scanner
        auth.invalidate()
        assert await auth.get_client() is second_scanner


@pytest.mark.asyncio
async def test_rate_limited_token_exchange_retries_once_and_succeeds():
    auth = GitHubAppInstallationAuth()
    rate_limited = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"Retry-After": "30"},
    )
    success = httpx.Response(
        201,
        json={"token": "token", "expires_at": "2999-01-01T00:00:00Z"},
    )
    first_exchange = Mock()
    first_exchange.request_with_result = AsyncMock(
        return_value=GitHubAPIResult(response=rate_limited)
    )
    second_exchange = Mock()
    second_exchange.request_with_result = AsyncMock(
        return_value=GitHubAPIResult(response=success)
    )
    scanner_client = Mock()

    with (
        patch.object(auth, "_app_jwt", return_value="app-jwt"),
        patch(
            "app.utils.github_app.GitHubAPIClient",
            side_effect=[first_exchange, second_exchange, scanner_client],
        ),
        patch("app.utils.github_app.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        patch("app.utils.github_app.settings.inactive_repos_github_app_id", 1),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_installation_id", 2
        ),
        patch(
            "app.utils.github_app.settings.inactive_repos_github_app_private_key_file",
            "/private-key.pem",
        ),
    ):
        assert await auth.get_client() is scanner_client

    mock_sleep.assert_awaited_once_with(30.0)
    assert second_exchange.request_with_result.await_count == 1
