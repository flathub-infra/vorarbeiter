from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt

from app.config import settings
from app.utils.github import GitHubAPIClient


class GitHubAppAuthenticationError(RuntimeError):
    pass


class GitHubAppInstallationAuth:
    REFRESH_MARGIN = timedelta(minutes=5)

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._client: GitHubAPIClient | None = None

    def _configuration(self) -> tuple[int, int, Path]:
        app_id = settings.inactive_repos_github_app_id
        installation_id = settings.inactive_repos_github_app_installation_id
        key_file = settings.inactive_repos_github_app_private_key_file
        missing = []
        if app_id is None:
            missing.append("INACTIVE_REPOS_GITHUB_APP_ID")
        if installation_id is None:
            missing.append("INACTIVE_REPOS_GITHUB_APP_INSTALLATION_ID")
        if not key_file:
            missing.append("INACTIVE_REPOS_GITHUB_APP_PRIVATE_KEY_FILE")
        if missing:
            raise GitHubAppAuthenticationError(
                f"Missing scanner configuration: {', '.join(missing)}"
            )
        assert app_id is not None
        assert installation_id is not None
        assert key_file is not None
        return app_id, installation_id, Path(key_file)

    def _app_jwt(self, app_id: int, key_file: Path) -> str:
        try:
            private_key = key_file.read_text(encoding="utf-8")
        except OSError as error:
            raise GitHubAppAuthenticationError(
                f"Cannot read scanner private key file: {key_file}"
            ) from error
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "iat": int((now - timedelta(seconds=60)).timestamp()),
                "exp": int((now + timedelta(minutes=9)).timestamp()),
                "iss": str(app_id),
            },
            private_key,
            algorithm="RS256",
        )

    async def _refresh(self) -> None:
        app_id, installation_id, key_file = self._configuration()
        app_jwt = self._app_jwt(app_id, key_file)
        auth_client = GitHubAPIClient(app_jwt, authorization_scheme="Bearer")
        result = await auth_client.request_with_result(
            "post",
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            raise_for_status=False,
        )
        response = result.response
        if response is None:
            raise GitHubAppAuthenticationError(
                f"GitHub App token exchange failed: {result.error_type or 'request error'}"
            )
        if response.status_code not in (200, 201):
            raise GitHubAppAuthenticationError(
                f"GitHub App token exchange failed with HTTP {response.status_code}"
            )
        try:
            body = response.json()
            token = body["token"]
            expires_at = datetime.fromisoformat(body["expires_at"])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise GitHubAppAuthenticationError(
                "GitHub App token exchange returned an unexpected response"
            ) from error
        if not isinstance(token, str) or not token or expires_at.tzinfo is None:
            raise GitHubAppAuthenticationError(
                "GitHub App token exchange returned an unexpected response"
            )
        self._token = token
        self._expires_at = expires_at.astimezone(UTC)
        self._client = GitHubAPIClient(token, authorization_scheme="Bearer")

    async def get_client(self) -> GitHubAPIClient:
        now = datetime.now(UTC)
        if (
            self._client is None
            or self._expires_at is None
            or self._expires_at <= now + self.REFRESH_MARGIN
        ):
            await self._refresh()
        if self._client is None:
            raise GitHubAppAuthenticationError(
                "GitHub App client initialization failed"
            )
        return self._client

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = None
        self._client = None
