import hashlib
import hmac
import json
import uuid
from unittest.mock import Mock, AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models import Pipeline, PipelineStatus
from app.routes.webhooks import is_submodule_only_pr
from app.models.webhook_event import WebhookEvent, WebhookSource
from tests.conftest import create_mock_get_db

# Sample GitHub payloads (simplified)
SAMPLE_GITHUB_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "opened",
    "pull_request": {
        "number": 123,
        "head": {
            "ref": "feature-branch",
            "sha": "abcdef123456",
        },
        "base": {
            "ref": "master",
        },
    },
}

# Sample payload for a push event to master
SAMPLE_PUSH_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "ref": "refs/heads/master",
    "commits": [{"id": "abc123"}],
}

# Sample payload for a comment with "bot, build"
SAMPLE_COMMENT_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "please bot, build this"},
}

# Sample payload that should be ignored
SAMPLE_IGNORED_PAYLOAD_1 = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "closed",
}

# Ignore bot, build inside a quoted comment (reply)
SAMPLE_IGNORED_PAYLOAD_2 = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "> foobar a long line.\r\n> \r\n> bot, build\r\n\r\nok!"},
}

# Ignore bot, build inside a code ticks
SAMPLE_IGNORED_PAYLOAD_3 = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "`bot, build`"},
}

# Ignore bot, build inside a code ticks
SAMPLE_IGNORED_PAYLOAD_4 = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "`I want to bot, build`"},
}

SAMPLE_IGNORED_PAYLOAD_5 = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "opened",
    "pull_request": {
        "number": 123,
        "head": {
            "ref": "feature-branch",
            "sha": "abcdef123456",
        },
        "base": {
            "ref": "abracadabra",
        },
    },
}

SAMPLE_IGNORED_BOT_PR_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "dependabot[bot]"},
    "action": "opened",
    "pull_request": {
        "number": 123,
        "head": {
            "ref": "feature-branch",
            "sha": "abcdef123456",
        },
        "base": {
            "ref": "main",
        },
    },
}

SAMPLE_GITHUB_ACTIONS_BOT_PAYLOAD = {
    "repository": {"full_name": "some-user/some-repo"},
    "sender": {"login": "github-actions[bot]"},
    "action": "created",
    "comment": {"body": "bot, build", "user": {"login": "github-actions[bot]"}},
}

SAMPLE_GITHUB_ACTIONS_BOT_FLATHUB_PAYLOAD = {
    "repository": {"full_name": "flathub/flathub"},
    "sender": {"login": "github-actions[bot]"},
    "action": "created",
    "comment": {"body": "bot, build", "user": {"login": "github-actions[bot]"}},
}

SAMPLE_ADMIN_PING_COMMENT_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "bot, ping admins"},
}


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.flush = AsyncMock()
    return mock_session


@pytest.fixture
def mock_db(mock_db_session):
    """Mock the database session factory."""

    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.get_db", mock_get_db):
        yield mock_db_session


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


def test_receive_github_webhook_success(client: TestClient, mock_db_session):
    """Test successful ingestion of a GitHub webhook."""
    delivery_id = str(uuid.uuid4())
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
            with patch(
                "app.routes.webhooks.is_eol_only_pr",
                AsyncMock(return_value=(False, None)),
            ):
                with patch("app.routes.webhooks.create_pipeline", return_value=None):
                    response = client.post(
                        "/api/webhooks/github",
                        json=SAMPLE_GITHUB_PAYLOAD,
                        headers=headers,
                    )

                    assert response.status_code == 202
                    response_data = response.json()
                    assert response_data["message"] == "Webhook received"
                    assert response_data["event_id"] == delivery_id

                    assert mock_db_session.add.called
                    assert mock_db_session.commit.called


def test_receive_github_webhook_missing_header(client: TestClient):
    """Test handling of missing GitHub delivery header."""
    response = client.post("/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD)

    assert response.status_code == 400
    assert "Missing X-GitHub-Delivery header" in response.json()["detail"]


def test_receive_github_webhook_invalid_header(client: TestClient):
    """Test handling of invalid GitHub delivery header."""
    headers = {"X-GitHub-Delivery": "not-a-uuid"}

    response = client.post(
        "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
    )

    assert response.status_code == 400
    assert "Invalid X-GitHub-Delivery header format" in response.json()["detail"]


def test_receive_github_webhook_invalid_json(client: TestClient):
    """Test handling of invalid JSON."""
    delivery_id = str(uuid.uuid4())
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
        response = client.post(
            "/api/webhooks/github", content="not-json", headers=headers
        )

        assert response.status_code == 400
        assert "Invalid JSON payload" in response.json()["detail"]


def test_receive_github_webhook_missing_keys(client: TestClient):
    """Test handling of missing keys in the payload."""
    delivery_id = str(uuid.uuid4())
    payload: dict[str, object] = {"sender": {"login": "user"}}
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
        response = client.post("/api/webhooks/github", json=payload, headers=headers)

        assert response.status_code == 422
        assert "Missing expected key in GitHub payload" in response.json()["detail"]


def test_receive_github_webhook_nested_key_error(client: TestClient):
    """Test handling of nested key error."""
    delivery_id = str(uuid.uuid4())
    payload: dict[str, dict[str, object]] = {"repository": {}, "sender": {}}
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
        response = client.post(
            "/api/webhooks/github",
            json=payload,  # type: ignore
            headers=headers,
        )

        assert response.status_code == 422
        assert "Missing expected key in GitHub payload" in response.json()["detail"]


def test_webhook_with_signature_verification_success(
    client: TestClient, mock_db_session
):
    """Test webhook signature verification."""
    # Save the original setting
    original_secret = settings.github_webhook_secret
    settings.github_webhook_secret = "test-secret"

    try:
        delivery_id = str(uuid.uuid4())
        payload = json.dumps(SAMPLE_GITHUB_PAYLOAD).encode()
        secret = settings.github_webhook_secret.encode()
        signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()

        headers = {
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": f"sha256={signature}",
            "Content-Type": "application/json",
        }

        mock_db_context = AsyncMock()
        mock_db_context.__aenter__.return_value = mock_db_session
        with (
            patch("app.routes.webhooks.get_db", return_value=mock_db_context),
            patch("app.routes.webhooks.create_pipeline", return_value=None),
            patch(
                "app.routes.webhooks.is_eol_only_pr",
                AsyncMock(return_value=(False, None)),
            ),
        ):
            response = client.post(
                "/api/webhooks/github", content=payload, headers=headers
            )

            assert response.status_code == 202
            assert "Webhook received" in response.json()["message"]
    finally:
        # Restore the original setting
        settings.github_webhook_secret = original_secret


def test_webhook_with_missing_signature(client: TestClient):
    """Test webhook with missing signature when secret is configured."""
    # Save the original setting
    original_secret = settings.github_webhook_secret
    settings.github_webhook_secret = "test-secret"

    try:
        delivery_id = str(uuid.uuid4())
        headers = {"X-GitHub-Delivery": delivery_id}

        response = client.post(
            "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
        )

        assert response.status_code == 401
        assert "Missing X-Hub-Signature-256 header" in response.json()["detail"]
    finally:
        # Restore the original setting
        settings.github_webhook_secret = original_secret


def test_webhook_with_invalid_signature(client: TestClient):
    """Test webhook with invalid signature."""
    # Save the original setting
    original_secret = settings.github_webhook_secret
    settings.github_webhook_secret = "test-secret"

    try:
        delivery_id = str(uuid.uuid4())
        headers = {
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": "sha256=invalid-signature",
        }

        response = client.post(
            "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
        )

        assert response.status_code == 401
        assert "Invalid signature" in response.json()["detail"]
    finally:
        # Restore the original setting
        settings.github_webhook_secret = original_secret


def test_should_store_event_admin_ping():
    from app.routes.webhooks import should_store_event

    assert should_store_event(SAMPLE_ADMIN_PING_COMMENT_PAYLOAD) is True


def test_should_store_event_pr_opened():
    """Test should_store_event returns True for PR opened."""
    from app.routes.webhooks import should_store_event

    payload = {
        "action": "opened",
        "pull_request": {"number": 123},
    }

    assert should_store_event(payload) is True


def test_should_store_event_pr_synchronize():
    """Test should_store_event returns True for PR synchronize."""
    from app.routes.webhooks import should_store_event

    payload = {
        "action": "synchronize",
        "pull_request": {"number": 123},
    }

    assert should_store_event(payload) is True


def test_should_store_event_push_to_master():
    """Test should_store_event returns True for push to master."""
    from app.routes.webhooks import should_store_event

    assert should_store_event(SAMPLE_PUSH_PAYLOAD) is True


def test_should_store_event_push_to_beta():
    """Test should_store_event returns True for push to beta."""
    from app.routes.webhooks import should_store_event

    payload = dict(SAMPLE_PUSH_PAYLOAD)
    payload["ref"] = "refs/heads/beta"

    assert should_store_event(payload) is True


def test_should_store_event_push_to_branch():
    """Test should_store_event returns True for push to branch/*."""
    from app.routes.webhooks import should_store_event

    payload = dict(SAMPLE_PUSH_PAYLOAD)
    payload["ref"] = "refs/heads/branch/feature-x"

    assert should_store_event(payload) is True


def test_should_store_event_comment_with_bot_build():
    """Test should_store_event returns True for comment with 'bot, build'."""
    from app.routes.webhooks import should_store_event

    assert should_store_event(SAMPLE_COMMENT_PAYLOAD) is True


def test_should_store_event_github_bot_flathub_repo():
    from app.routes.webhooks import should_store_event

    assert should_store_event(SAMPLE_GITHUB_ACTIONS_BOT_FLATHUB_PAYLOAD) is True


def test_should_not_store_event():
    """Test should_store_event returns False for other events."""
    from app.routes.webhooks import should_store_event

    payloads = [
        SAMPLE_IGNORED_PAYLOAD_1,
        SAMPLE_IGNORED_PAYLOAD_2,
        SAMPLE_IGNORED_PAYLOAD_3,
        SAMPLE_IGNORED_PAYLOAD_4,
        SAMPLE_IGNORED_PAYLOAD_5,
        SAMPLE_GITHUB_ACTIONS_BOT_PAYLOAD,
    ]

    for payload in payloads:
        assert should_store_event(payload) is False


def test_receive_github_webhook_ignore_event(client: TestClient, mock_db_session):
    """Test that events not matching criteria are received but not stored."""
    delivery_id = str(uuid.uuid4())
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
        response = client.post(
            "/api/webhooks/github", json=SAMPLE_IGNORED_PAYLOAD_1, headers=headers
        )

        assert response.status_code == 202
        response_data = response.json()
        assert response_data["message"] == "Webhook received"
        assert response_data["event_id"] == delivery_id

        mock_db_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_receive_github_webhook_ignores_bot_pr(client, mock_db_session):
    headers = {"X-GitHub-Delivery": str(uuid.uuid4())}

    with (
        patch("app.routes.webhooks.settings.github_webhook_secret", ""),
        patch("app.routes.webhooks.get_db", return_value=AsyncMock()),
    ):
        response = client.post(
            "/api/webhooks/github",
            json=SAMPLE_IGNORED_BOT_PR_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 202
        assert "ignored due to actor filter" in response.json()["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mock_files_response,expected",
    [
        (  # Submodule-only PR
            [
                {
                    "filename": "submodule-dir",
                    "patch": "@@ -1 +1 @@\n-Subproject commit f21291a3b3440182699ef5ef5d228046f08dd66c\n+Subproject commit 65374641cdedb0664879196b62127f76a106185a",
                },
            ],
            True,
        ),
        (  # Normal PR
            [
                {"filename": "file1.txt", "patch": "Some content"},
                {"filename": "file2.txt", "patch": "Other content"},
            ],
            False,
        ),
        (  # Empty files list
            [],
            False,
        ),
        (  # Submodule and normal file
            [
                {
                    "filename": "submodule-dir",
                    "patch": "@@ -1 +1 @@\n-Subproject commit abc\n+Subproject commit def",
                },
                {"filename": "file.txt", "patch": "Some content"},
            ],
            False,
        ),
    ],
)
async def test_is_submodule_only_pr(mock_files_response, expected):
    payload = {
        "repository": {"full_name": "test-owner/test-repo"},
        "pull_request": {"number": 123},
    }

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = Mock(return_value=mock_files_response)
    mock_response.raise_for_status = Mock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        result = await is_submodule_only_pr(payload)
        assert result == expected


@pytest.mark.asyncio
async def test_receive_github_webhook_ignores_submodule_only_pr(
    client, mock_db_session
):
    headers = {"X-GitHub-Delivery": str(uuid.uuid4())}

    sample_payload = {
        "repository": {"full_name": "test-owner/test-repo"},
        "sender": {"login": "github-actions[bot]"},
        "action": "synchronize",
        "pull_request": {"number": 123},
    }

    with (
        patch("app.routes.webhooks.settings.github_webhook_secret", ""),
        patch("app.routes.webhooks.get_db", return_value=AsyncMock()),
        patch("app.routes.webhooks.is_submodule_only_pr", AsyncMock(return_value=True)),
    ):
        response = client.post(
            "/api/webhooks/github", json=sample_payload, headers=headers
        )

        assert response.status_code == 202
        data = response.json()
        assert "ignored due to PR changes filter" in data["message"]


@pytest.mark.asyncio
async def test_fetch_flathub_json_success():
    from app.routes.webhooks import fetch_flathub_json

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = Mock(
        return_value={"end-of-life": "This application is no longer maintained."}
    )
    mock_response.raise_for_status = Mock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        result = await fetch_flathub_json(
            "test-owner/test-repo", "abc123", "test-token"
        )

    assert result == {"end-of-life": "This application is no longer maintained."}
    mock_client_instance.get.assert_awaited_once_with(
        "https://api.github.com/repos/test-owner/test-repo/contents/flathub.json?ref=abc123",
        headers={
            "Accept": "application/vnd.github.raw+json",
            "Authorization": "Bearer test-token",
        },
    )


@pytest.mark.asyncio
async def test_fetch_flathub_json_missing_file():
    from app.routes.webhooks import fetch_flathub_json

    mock_response = AsyncMock()
    mock_response.status_code = 404
    mock_response.raise_for_status = Mock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        result = await fetch_flathub_json("test-owner/test-repo", "abc123")

    assert result == {}
    mock_response.raise_for_status.assert_not_called()


@pytest.mark.parametrize(
    "base_json,head_json,expected_data",
    [
        (
            {
                "end-of-life": "This application has been replaced by org.old.App.",
                "end-of-life-rebase": "org.old.App",
            },
            {
                "end-of-life": "This application has been replaced by org.new.App.",
                "end-of-life-rebase": "org.new.App",
            },
            {
                "end_of_life": "This application has been replaced by org.new.App.",
                "end_of_life_rebase": "org.new.App",
            },
        ),
        (
            {"end-of-life": "This application is no longer maintained."},
            {"end-of-life": ""},
            {"end_of_life": ""},
        ),
        (
            {"name": "Old", "end-of-life": "This application is no longer maintained."},
            {"name": "New", "end-of-life": "This application is no longer maintained."},
            None,
        ),
        (
            {"end-of-life-rebase": "org.old.App"},
            {},
            {"end_of_life_rebase": ""},
        ),
        (
            {
                "name": "Same",
                "end-of-life": "This application is no longer maintained.",
            },
            {
                "name": "Same",
                "end-of-life": "This application is no longer maintained.",
            },
            None,
        ),
    ],
)
def test_get_eol_only_changes(base_json, head_json, expected_data):
    from app.routes.webhooks import get_eol_only_changes

    result = get_eol_only_changes(base_json, head_json)

    assert result == expected_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mock_files_response,base_json,head_json,expected_result,expected_data",
    [
        (
            [
                {
                    "filename": "flathub.json",
                }
            ],
            {
                "end-of-life": "This application has been replaced by org.old.App.",
                "end-of-life-rebase": "org.old.App",
            },
            {
                "end-of-life": "This application has been replaced by org.new.App.",
                "end-of-life-rebase": "org.new.App",
            },
            True,
            {
                "end_of_life": "This application has been replaced by org.new.App.",
                "end_of_life_rebase": "org.new.App",
            },
        ),
        (
            [
                {
                    "filename": "flathub.json",
                }
            ],
            {"name": "Old", "end-of-life": "This application is no longer maintained."},
            {"name": "New", "end-of-life": "This application is no longer maintained."},
            False,
            None,
        ),
        (
            [
                {"filename": "flathub.json", "patch": "foo"},
                {"filename": "other.json", "patch": "bar"},
            ],
            {},
            {},
            False,
            None,
        ),
    ],
)
async def test_is_eol_only_pr(
    mock_files_response, base_json, head_json, expected_result, expected_data
):
    from app.routes.webhooks import is_eol_only_pr

    payload = {
        "repository": {"full_name": "test-owner/test-repo"},
        "pull_request": {
            "number": 123,
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha"},
        },
    }

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = Mock(return_value=mock_files_response)
    mock_response.raise_for_status = Mock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        with patch(
            "app.routes.webhooks.fetch_flathub_json",
            AsyncMock(side_effect=[base_json, head_json]),
        ) as mock_fetch:
            result, data = await is_eol_only_pr(payload)
            assert result is expected_result
            assert data == expected_data
            if (
                mock_files_response
                and len(mock_files_response) == 1
                and mock_files_response[0].get("filename") == "flathub.json"
            ):
                assert mock_fetch.await_count == 2
            else:
                assert mock_fetch.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mock_files_response,base_json,head_json,expected_result,expected_data",
    [
        (
            [
                {
                    "filename": "flathub.json",
                }
            ],
            {
                "end-of-life": "This application has been replaced by org.old.App.",
                "end-of-life-rebase": "org.old.App",
            },
            {
                "end-of-life": "This application has been replaced by org.new.App.",
                "end-of-life-rebase": "org.new.App",
            },
            True,
            {
                "end_of_life": "This application has been replaced by org.new.App.",
                "end_of_life_rebase": "org.new.App",
            },
        ),
        (
            [
                {
                    "filename": "flathub.json",
                }
            ],
            {"name": "Old", "end-of-life": "This application is no longer maintained."},
            {"name": "New", "end-of-life": "This application is no longer maintained."},
            False,
            None,
        ),
        (
            [
                {"filename": "flathub.json", "patch": "foo"},
                {"filename": "other.json", "patch": "bar"},
            ],
            {},
            {},
            False,
            None,
        ),
    ],
)
async def test_is_eol_only_push(
    mock_files_response, base_json, head_json, expected_result, expected_data
):
    from app.routes.webhooks import is_eol_only_push

    payload = {
        "repository": {"full_name": "test-owner/test-repo"},
        "before": "abc123",
        "after": "def456",
    }

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = Mock(return_value={"files": mock_files_response})
    mock_response.raise_for_status = Mock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        with patch(
            "app.routes.webhooks.fetch_flathub_json",
            AsyncMock(side_effect=[base_json, head_json]),
        ) as mock_fetch:
            result, data = await is_eol_only_push(payload)
            assert result is expected_result
            assert data == expected_data
            if (
                mock_files_response
                and len(mock_files_response) == 1
                and mock_files_response[0].get("filename") == "flathub.json"
            ):
                assert mock_fetch.await_count == 2
            else:
                assert mock_fetch.await_count == 0


@pytest.mark.asyncio
async def test_is_eol_only_push_skips_zero_sha():
    from app.routes.webhooks import is_eol_only_push

    payload = {
        "repository": {"full_name": "test-owner/test-repo"},
        "before": "0" * 40,
        "after": "def456",
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        result, data = await is_eol_only_push(payload)

    assert result is False
    assert data is None
    assert not mock_client_class.called


@pytest.mark.asyncio
async def test_handle_eol_only_pr_posts_status_and_comment():
    from app.routes.webhooks import handle_eol_only_pr

    payload = {
        "repository": {"full_name": "test-owner/test-repo"},
        "pull_request": {
            "number": 123,
            "head": {"sha": "abcdef123456"},
        },
    }
    eol_data = {
        "end_of_life": "This application has been replaced by org.new.App.",
        "end_of_life_rebase": "org.new.App",
    }

    with patch("app.routes.webhooks.update_commit_status", AsyncMock()) as mock_status:
        with patch(
            "app.routes.webhooks.create_pr_comment", AsyncMock()
        ) as mock_comment:
            await handle_eol_only_pr(payload, eol_data)

            assert mock_status.await_args is not None
            assert mock_status.await_args.kwargs["state"] == "success"
            assert (
                mock_status.await_args.kwargs["description"]
                == "EOL-only change - build skipped"
            )

            assert mock_comment.await_args is not None
            comment = mock_comment.await_args.kwargs["comment"]
            assert "EOL-only change" in comment
            assert "This application has been replaced by org.new.App." in comment
            assert "org.new.App" in comment


@pytest.mark.asyncio
async def test_handle_eol_only_push_republish():
    from app.routes.webhooks import handle_eol_only_push

    event = WebhookEvent(
        id=uuid.uuid4(),
        source=WebhookSource.GITHUB,
        payload={},
        repository="test-owner/test-repo",
        actor="test-actor",
    )
    eol_data = {"end_of_life": "This application is no longer maintained."}

    mock_client = AsyncMock()
    mock_client.republish = AsyncMock(return_value={"id": 12345, "status": "ok"})

    with patch(
        "app.routes.webhooks.FlatManagerClient", return_value=mock_client
    ) as mock_client_class:
        with patch(
            "app.routes.webhooks.update_commit_status", AsyncMock()
        ) as mock_status:
            await handle_eol_only_push(
                event, "refs/heads/master", "abcdef123456", eol_data
            )

            mock_client_class.assert_called_once_with(
                url=settings.flat_manager_url,
                token=settings.flat_manager_token,
            )
            mock_client.republish.assert_awaited_once_with(
                repo="stable",
                app_id="test-repo",
                end_of_life="This application is no longer maintained.",
                end_of_life_rebase=None,
            )

            assert mock_status.await_args_list[0].kwargs["state"] == "pending"
            assert mock_status.await_args_list[1].kwargs["state"] == "success"
            assert (
                mock_status.await_args_list[1].kwargs["target_url"]
                == f"{settings.flat_manager_url}/status/12345"
            )


@pytest.mark.asyncio
async def test_create_pipeline_pr():
    """Test creating a pipeline from a PR webhook event."""
    event_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=SAMPLE_GITHUB_PAYLOAD,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.flathub.test-repo",
        params={},
        webhook_event_id=event_id,
        status=PipelineStatus.PENDING,
    )

    mock_pipeline_service = AsyncMock()
    mock_pipeline_service.create_pipeline.return_value = mock_pipeline
    mock_pipeline_service.start_pipeline.return_value = mock_pipeline

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.BuildPipeline", return_value=mock_pipeline_service):
        with patch("app.routes.webhooks.get_db", mock_get_db):
            with patch("app.pipelines.build.get_db", mock_get_db):
                from app.routes.webhooks import create_pipeline

                result = await create_pipeline(webhook_event)

                assert result == pipeline_id

                mock_pipeline_service.create_pipeline.assert_called_once()

                mock_pipeline_service.start_pipeline.assert_called_once_with(
                    pipeline_id=pipeline_id
                )


@pytest.mark.asyncio
async def test_create_pipeline_push():
    """Test creating a pipeline from a push webhook event."""
    event_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    modified_payload = dict(SAMPLE_PUSH_PAYLOAD)
    modified_payload["after"] = "abcdef123456"

    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=modified_payload,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.flathub.test-repo",
        params={},
        webhook_event_id=event_id,
        status=PipelineStatus.PENDING,
    )

    mock_pipeline_service = AsyncMock()
    mock_pipeline_service.create_pipeline.return_value = mock_pipeline
    mock_pipeline_service.start_pipeline.return_value = mock_pipeline

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.BuildPipeline", return_value=mock_pipeline_service):
        with patch("app.routes.webhooks.get_db", mock_get_db):
            with patch("app.pipelines.build.get_db", mock_get_db):
                with patch(
                    "app.routes.webhooks.is_eol_only_push",
                    AsyncMock(return_value=(False, None)),
                ):
                    from app.routes.webhooks import create_pipeline

                    result = await create_pipeline(webhook_event)

                    assert result == pipeline_id

                    # Verify the parameters passed to create_pipeline
                    args, kwargs = mock_pipeline_service.create_pipeline.call_args
                    assert "params" in kwargs
                    assert kwargs["params"].get("ref") == "refs/heads/master"
                    assert kwargs["params"].get("push") == "true"

                    assert mock_pipeline_service.create_pipeline.called
                    assert mock_pipeline_service.start_pipeline.called
                    assert isinstance(result, uuid.UUID)


@pytest.mark.asyncio
async def test_create_pipeline_comment():
    """Test creating a pipeline from a comment webhook event."""
    event_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    comment_payload = dict(SAMPLE_COMMENT_PAYLOAD)
    comment_payload["issue"] = {
        "number": 42,
        "pull_request": {
            "url": "https://api.github.com/repos/test-owner/test-repo/pulls/42"
        },
    }
    comment_payload["comment"] = {
        "body": "please bot, build this",
        "id": 12345,
        "user": {"login": "test-user"},
        "html_url": "https://github.com/test-owner/test-repo/pull/42#comment-12345",
    }
    comment_payload["after"] = "fedcba654321"

    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=comment_payload,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="org.flathub.test-repo",
        params={},
        webhook_event_id=event_id,
        status=PipelineStatus.PENDING,
    )

    mock_pipeline_service = AsyncMock()
    mock_pipeline_service.create_pipeline.return_value = mock_pipeline
    mock_pipeline_service.start_pipeline.return_value = mock_pipeline

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline

    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.BuildPipeline", return_value=mock_pipeline_service):
        with patch("app.routes.webhooks.get_db", mock_get_db):
            with patch("app.pipelines.build.get_db", mock_get_db):
                from app.routes.webhooks import create_pipeline

                result = await create_pipeline(webhook_event)

                assert result == pipeline_id

                args, kwargs = mock_pipeline_service.create_pipeline.call_args
                assert "params" in kwargs
                assert kwargs["params"].get("pr_number") == "42"
                assert kwargs["params"].get("ref") == "refs/pull/42/head"

                assert mock_pipeline_service.create_pipeline.called
                assert mock_pipeline_service.start_pipeline.called
                assert isinstance(result, uuid.UUID)


@pytest.mark.asyncio
async def test_receive_webhook_creates_pipeline(client, mock_db_session):
    """Test that PR webhook events create pipelines."""
    delivery_id = str(uuid.uuid4())
    pipeline_id = uuid.uuid4()
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch(
            "app.routes.webhooks.create_pipeline",
            AsyncMock(return_value=pipeline_id),
        ):
            with patch(
                "app.routes.webhooks.is_eol_only_pr",
                AsyncMock(return_value=(False, None)),
            ):
                with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
                    response = client.post(
                        "/api/webhooks/github",
                        json=SAMPLE_GITHUB_PAYLOAD,
                        headers=headers,
                    )

                    assert response.status_code == 202
                    response_data = response.json()
                    assert response_data["message"] == "Webhook received"
                    assert response_data["event_id"] == delivery_id
                    assert response_data["pipeline_id"] == str(pipeline_id)

                    assert mock_db_session.add.called
                    assert mock_db_session.commit.called


@pytest.mark.asyncio
async def test_create_pipeline_admin_ping():
    event_id = uuid.uuid4()
    comment_payload = dict(SAMPLE_COMMENT_PAYLOAD)
    comment_payload["issue"] = {
        "number": 99,
        "pull_request": {
            "url": "https://api.github.com/repos/test-owner/test-repo/pulls/99"
        },
    }
    comment_payload["comment"] = {
        "body": "bot, ping admins",
        "id": 54321,
        "user": {"login": "test-user"},
        "html_url": "https://github.com/test-owner/test-repo/pull/99#comment-54321",
    }

    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=comment_payload,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_get_db = create_mock_get_db(mock_db_session)

    with (
        patch(
            "app.routes.webhooks.add_issue_comment", new_callable=AsyncMock
        ) as mock_add_comment,
        patch("app.routes.webhooks.get_db", mock_get_db),
    ):
        from app.routes.webhooks import create_pipeline

        result = await create_pipeline(webhook_event)

        assert result is None

        mock_add_comment.assert_awaited_once_with(
            git_repo="test-owner/test-repo",
            issue_number=99,
            comment="Contacted Flathub admins: cc @flathub/build-moderation",
            check_duplicates=True,
        )


# Test data for retry functionality
SAMPLE_ISSUE_BODY_STABLE = """The stable build pipeline for `test-app` failed.

Commit SHA: abc123456789
Build log: https://github.com/flathub-infra/vorarbeiter/actions/runs/123456789"""

SAMPLE_ISSUE_BODY_JOB_FAILURE = """The commit job for `test-app` failed in the stable repository.

**Build Information:**
- Commit SHA: abc123456789
- Build ID: 456
- Build log: https://example.com/log/123

**Job Details:**
- Job ID: 789
- Job status: https://hub.flathub.org/status/789

cc @flathub/build-moderation"""

SAMPLE_RETRY_COMMENT_PAYLOAD = {
    "repository": {"full_name": "flathub/test-app"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "bot, retry", "user": {"login": "test-user"}},
    "issue": {
        "number": 123,
        "body": SAMPLE_ISSUE_BODY_STABLE,
        "state": "open",
        "user": {"login": "flathubbot"},
    },
}


@pytest.mark.asyncio
async def test_parse_failure_issue_stable_build():
    from app.routes.webhooks import parse_failure_issue

    result = await parse_failure_issue(SAMPLE_ISSUE_BODY_STABLE, "flathub/test-app")

    assert result is not None
    assert result["sha"] == "abc123456789"
    assert result["repo"] == "flathub/test-app"
    assert result["ref"] == "refs/heads/master"
    assert result["flat_manager_repo"] == "stable"
    assert result["issue_type"] == "build_failure"


@pytest.mark.asyncio
async def test_parse_failure_issue_job_failure():
    from app.routes.webhooks import parse_failure_issue

    result = await parse_failure_issue(
        SAMPLE_ISSUE_BODY_JOB_FAILURE, "flathub/test-app"
    )

    assert result is not None
    assert result["sha"] == "abc123456789"
    assert result["repo"] == "flathub/test-app"
    assert result["ref"] == "refs/heads/master"
    assert result["flat_manager_repo"] == "stable"
    assert result["issue_type"] == "job_failure"
    assert result["job_type"] == "commit"


@pytest.mark.asyncio
async def test_parse_failure_issue_invalid():
    from app.routes.webhooks import parse_failure_issue

    invalid_body = "This is not a build failure issue."
    result = await parse_failure_issue(invalid_body, "flathub/test-app")

    assert result is None


def test_should_store_event_bot_retry():
    from app.routes.webhooks import should_store_event

    payload = {"comment": {"body": "bot, retry"}, "action": "created"}

    assert should_store_event(payload) is True


def test_should_store_event_bot_retry_case_insensitive():
    from app.routes.webhooks import should_store_event

    payload = {"comment": {"body": "Bot, Retry"}, "action": "created"}

    assert should_store_event(payload) is True


@pytest.mark.asyncio
async def test_validate_retry_permissions_collaborator():
    from app.routes.webhooks import validate_retry_permissions

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        with patch("app.routes.webhooks.settings.github_status_token", "test-token"):
            result = await validate_retry_permissions("flathub/test-app", "test-user")

            assert result is True


@pytest.mark.asyncio
async def test_validate_retry_permissions_org_member():
    from app.routes.webhooks import validate_retry_permissions

    with patch("httpx.AsyncClient") as MockClient:
        collab_response = MagicMock()
        collab_response.status_code = 404

        org_response = MagicMock()
        org_response.status_code = 204

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(
            side_effect=[collab_response, org_response]
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        with patch("app.routes.webhooks.settings.github_status_token", "test-token"):
            result = await validate_retry_permissions("flathub/test-app", "test-user")

            assert result is True


@pytest.mark.asyncio
async def test_validate_retry_permissions_denied():
    from app.routes.webhooks import validate_retry_permissions

    with patch("httpx.AsyncClient") as MockClient:
        collab_response = MagicMock()
        collab_response.status_code = 404

        org_response = MagicMock()
        org_response.status_code = 404

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(
            side_effect=[collab_response, org_response]
        )
        MockClient.return_value.__aenter__.return_value = mock_client_instance

        with patch("app.routes.webhooks.settings.github_status_token", "test-token"):
            result = await validate_retry_permissions("flathub/test-app", "test-user")

            assert result is False


@pytest.mark.asyncio
async def test_handle_issue_retry_success():
    from app.routes.webhooks import handle_issue_retry

    event_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="test-app",
        params={"retry_count": 1, "retry_from_issue": 123},
        status=PipelineStatus.PENDING,
    )

    mock_pipeline_service = AsyncMock()
    mock_pipeline_service.create_pipeline.return_value = mock_pipeline
    mock_pipeline_service.start_pipeline.return_value = mock_pipeline

    with (
        patch("app.routes.webhooks.validate_retry_permissions", return_value=True),
        patch("app.routes.webhooks.is_issue_edited", AsyncMock(return_value=False)),
        patch(
            "app.routes.webhooks.get_workflow_run_title",
            AsyncMock(return_value="Build from refs/heads/master"),
        ),
        patch("app.routes.webhooks.BuildPipeline", return_value=mock_pipeline_service),
        patch("app.routes.webhooks.update_commit_status", AsyncMock()),
        patch("app.routes.webhooks.add_issue_comment", AsyncMock()),
        patch("app.routes.webhooks.close_github_issue", AsyncMock()),
    ):
        result = await handle_issue_retry(
            git_repo="flathub/test-app",
            issue_number=123,
            issue_body=SAMPLE_ISSUE_BODY_STABLE,
            comment_author="test-user",
            webhook_event_id=event_id,
        )

        assert result == pipeline_id
        mock_pipeline_service.create_pipeline.assert_called_once()
        mock_pipeline_service.start_pipeline.assert_called_once()


@pytest.mark.asyncio
async def test_handle_issue_retry_permission_denied():
    from app.routes.webhooks import handle_issue_retry

    event_id = uuid.uuid4()

    with (
        patch("app.routes.webhooks.validate_retry_permissions", return_value=False),
        patch("app.routes.webhooks.is_issue_edited", AsyncMock(return_value=False)),
        patch(
            "app.routes.webhooks.get_workflow_run_title", AsyncMock(return_value=None)
        ),
    ):
        with patch(
            "app.routes.webhooks.add_issue_comment", AsyncMock()
        ) as mock_comment:
            result = await handle_issue_retry(
                git_repo="flathub/test-app",
                issue_number=123,
                issue_body=SAMPLE_ISSUE_BODY_STABLE,
                comment_author="unauthorized-user",
                webhook_event_id=event_id,
            )

            assert result is None
            mock_comment.assert_called_once()
            args, kwargs = mock_comment.call_args
            assert "does not have permission" in kwargs["comment"]


@pytest.mark.asyncio
async def test_handle_issue_retry_invalid_issue():
    from app.routes.webhooks import handle_issue_retry

    event_id = uuid.uuid4()
    invalid_body = "This is not a build failure issue."

    with (
        patch("app.routes.webhooks.validate_retry_permissions", return_value=True),
        patch("app.routes.webhooks.is_issue_edited", AsyncMock(return_value=False)),
        patch(
            "app.routes.webhooks.get_workflow_run_title", AsyncMock(return_value=None)
        ),
    ):
        with patch(
            "app.routes.webhooks.add_issue_comment", AsyncMock()
        ) as mock_comment:
            result = await handle_issue_retry(
                git_repo="flathub/test-app",
                issue_number=123,
                issue_body=invalid_body,
                comment_author="test-user",
                webhook_event_id=event_id,
            )

            assert result is None
            mock_comment.assert_called_once()
            args, kwargs = mock_comment.call_args
            assert "Could not parse build parameters" in kwargs["comment"]


SAMPLE_CLOSED_PR_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "synchronize",
    "pull_request": {
        "number": 123,
        "state": "closed",
        "head": {
            "ref": "feature-branch",
            "sha": "abcdef123456",
        },
        "base": {
            "ref": "main",
        },
    },
}

SAMPLE_COMMENT_CLOSED_PR_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "created",
    "comment": {"body": "please bot, build this"},
    "issue": {
        "number": 42,
        "pull_request": {
            "url": "https://api.github.com/repos/test-owner/test-repo/pulls/42"
        },
    },
}


@pytest.mark.asyncio
async def test_create_pipeline_pr_closed_state():
    """Test that create_pipeline returns None for closed PR events."""
    event_id = uuid.uuid4()
    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=SAMPLE_CLOSED_PR_PAYLOAD,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.routes.webhooks.logger") as mock_logger:
            from app.routes.webhooks import create_pipeline

            result = await create_pipeline(webhook_event)

            assert result is None
            mock_logger.info.assert_called_once_with(
                "PR is closed, skipping pipeline creation",
                pr_number=123,
                repo="test-owner/test-repo",
                action="synchronize",
            )


@pytest.mark.asyncio
async def test_create_pipeline_pr_opened_closed_state():
    """Test that create_pipeline returns None for opened action on closed PR."""
    event_id = uuid.uuid4()

    closed_pr_payload = dict(SAMPLE_CLOSED_PR_PAYLOAD)
    closed_pr_payload["action"] = "opened"

    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=closed_pr_payload,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.routes.webhooks.logger") as mock_logger:
            from app.routes.webhooks import create_pipeline

            result = await create_pipeline(webhook_event)

            assert result is None
            mock_logger.info.assert_called_once_with(
                "PR is closed, skipping pipeline creation",
                pr_number=123,
                repo="test-owner/test-repo",
                action="opened",
            )


@pytest.mark.asyncio
async def test_create_pipeline_pr_reopened_closed_state():
    """Test that create_pipeline returns None for reopened action on closed PR."""
    event_id = uuid.uuid4()

    closed_pr_payload = dict(SAMPLE_CLOSED_PR_PAYLOAD)
    closed_pr_payload["action"] = "reopened"

    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=closed_pr_payload,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_get_db = create_mock_get_db(mock_db_session)

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.routes.webhooks.logger") as mock_logger:
            from app.routes.webhooks import create_pipeline

            result = await create_pipeline(webhook_event)

            assert result is None
            mock_logger.info.assert_called_once_with(
                "PR is closed, skipping pipeline creation",
                pr_number=123,
                repo="test-owner/test-repo",
                action="reopened",
            )


@pytest.mark.asyncio
async def test_create_pipeline_bot_build_closed_pr():
    """Test that create_pipeline returns None and posts comment for 'bot, build' on closed PR."""
    event_id = uuid.uuid4()
    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=SAMPLE_COMMENT_CLOSED_PR_PAYLOAD,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_get_db = create_mock_get_db(mock_db_session)

    mock_pr_response = {
        "number": 42,
        "state": "closed",
        "head": {"sha": "abcdef123456"},
    }

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.routes.webhooks.logger") as mock_logger:
            with patch(
                "app.routes.webhooks.create_pr_comment", AsyncMock()
            ) as mock_comment:
                with patch(
                    "app.routes.webhooks.settings.github_status_token", "test-token"
                ):
                    with patch("httpx.AsyncClient") as MockClient:
                        mock_response = MagicMock()
                        mock_response.json.return_value = mock_pr_response
                        mock_response.raise_for_status.return_value = None

                        mock_client_instance = AsyncMock()
                        mock_client_instance.get = AsyncMock(return_value=mock_response)
                        MockClient.return_value.__aenter__.return_value = (
                            mock_client_instance
                        )

                        from app.routes.webhooks import create_pipeline

                        result = await create_pipeline(webhook_event)

                        assert result is None
                        mock_logger.info.assert_called_once_with(
                            "PR is closed/merged, ignoring 'bot, build' command",
                            pr_number=42,
                            repo="test-owner/test-repo",
                            pr_state="closed",
                        )
                        mock_comment.assert_called_once_with(
                            git_repo="test-owner/test-repo",
                            pr_number=42,
                            comment="❌ Cannot build closed or merged PR. Please reopen the PR if you want to trigger a build.",
                        )


@pytest.mark.asyncio
async def test_create_pipeline_bot_build_merged_pr():
    """Test that create_pipeline returns None and posts comment for 'bot, build' on merged PR."""
    event_id = uuid.uuid4()
    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=SAMPLE_COMMENT_CLOSED_PR_PAYLOAD,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_get_db = create_mock_get_db(mock_db_session)

    mock_pr_response = {
        "number": 42,
        "state": "merged",
        "head": {"sha": "abcdef123456"},
    }

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.routes.webhooks.logger") as mock_logger:
            with patch(
                "app.routes.webhooks.create_pr_comment", AsyncMock()
            ) as mock_comment:
                with patch(
                    "app.routes.webhooks.settings.github_status_token", "test-token"
                ):
                    with patch("httpx.AsyncClient") as MockClient:
                        mock_response = MagicMock()
                        mock_response.json.return_value = mock_pr_response
                        mock_response.raise_for_status.return_value = None

                        mock_client_instance = AsyncMock()
                        mock_client_instance.get = AsyncMock(return_value=mock_response)
                        MockClient.return_value.__aenter__.return_value = (
                            mock_client_instance
                        )

                        from app.routes.webhooks import create_pipeline

                        result = await create_pipeline(webhook_event)

                        assert result is None
                        mock_logger.info.assert_called_once_with(
                            "PR is closed/merged, ignoring 'bot, build' command",
                            pr_number=42,
                            repo="test-owner/test-repo",
                            pr_state="merged",
                        )
                        mock_comment.assert_called_once_with(
                            git_repo="test-owner/test-repo",
                            pr_number=42,
                            comment="❌ Cannot build closed or merged PR. Please reopen the PR if you want to trigger a build.",
                        )


@pytest.mark.asyncio
async def test_create_pipeline_bot_build_open_pr_continues():
    """Test that create_pipeline continues normally for 'bot, build' on open PR."""
    event_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    webhook_event = WebhookEvent(
        id=event_id,
        source=WebhookSource.GITHUB,
        payload=SAMPLE_COMMENT_CLOSED_PR_PAYLOAD,
        repository="test-owner/test-repo",
        actor="test-actor",
    )

    mock_pipeline = Pipeline(
        id=pipeline_id,
        app_id="test-repo",
        params={"pr_number": "42", "ref": "refs/pull/42/head"},
        webhook_event_id=event_id,
        status=PipelineStatus.PENDING,
    )

    mock_pipeline_service = AsyncMock()
    mock_pipeline_service.create_pipeline.return_value = mock_pipeline
    mock_pipeline_service.start_pipeline.return_value = mock_pipeline

    mock_db_session = AsyncMock(spec=AsyncSession)
    mock_db_session.get.return_value = mock_pipeline
    mock_get_db = create_mock_get_db(mock_db_session)

    mock_pr_response = {"number": 42, "state": "open", "head": {"sha": "abcdef123456"}}

    with patch("app.routes.webhooks.get_db", mock_get_db):
        with patch("app.pipelines.build.get_db", mock_get_db):
            with patch(
                "app.routes.webhooks.BuildPipeline", return_value=mock_pipeline_service
            ):
                with patch("app.routes.webhooks.update_commit_status", AsyncMock()):
                    with patch("app.routes.webhooks.create_pr_comment", AsyncMock()):
                        with patch(
                            "app.routes.webhooks.settings.github_status_token",
                            "test-token",
                        ):
                            with patch("httpx.AsyncClient") as MockClient:
                                mock_response = MagicMock()
                                mock_response.json.return_value = mock_pr_response
                                mock_response.raise_for_status.return_value = None

                                mock_client_instance = AsyncMock()
                                mock_client_instance.get = AsyncMock(
                                    return_value=mock_response
                                )
                                MockClient.return_value.__aenter__.return_value = (
                                    mock_client_instance
                                )

                                from app.routes.webhooks import create_pipeline

                                result = await create_pipeline(webhook_event)

                                assert result == pipeline_id
                                mock_pipeline_service.create_pipeline.assert_called_once()
                                mock_pipeline_service.start_pipeline.assert_called_once()


@pytest.mark.asyncio
async def test_handle_eol_only_push_beta_ref():
    """Test that beta ref uses 'beta' flat-manager repo."""
    from app.routes.webhooks import handle_eol_only_push

    event = WebhookEvent(
        id=uuid.uuid4(),
        source=WebhookSource.GITHUB,
        payload={},
        repository="test-owner/test-repo",
        actor="test-actor",
    )
    eol_data = {"end_of_life": "This application is no longer maintained."}

    mock_client = AsyncMock()
    mock_client.republish = AsyncMock(return_value={"status": "ok"})

    with patch("app.routes.webhooks.FlatManagerClient", return_value=mock_client):
        with patch("app.routes.webhooks.update_commit_status", AsyncMock()):
            await handle_eol_only_push(
                event, "refs/heads/beta", "abcdef123456", eol_data
            )

            mock_client.republish.assert_awaited_once_with(
                repo="beta",
                app_id="test-repo",
                end_of_life="This application is no longer maintained.",
                end_of_life_rebase=None,
            )


@pytest.mark.asyncio
async def test_handle_eol_only_push_branch_ref():
    """Test that branch/* refs use 'stable' flat-manager repo."""
    from app.routes.webhooks import handle_eol_only_push

    event = WebhookEvent(
        id=uuid.uuid4(),
        source=WebhookSource.GITHUB,
        payload={},
        repository="test-owner/test-repo",
        actor="test-actor",
    )
    eol_data = {"end_of_life": "This application is no longer maintained."}

    mock_client = AsyncMock()
    mock_client.republish = AsyncMock(return_value={"status": "ok"})

    with patch("app.routes.webhooks.FlatManagerClient", return_value=mock_client):
        with patch("app.routes.webhooks.update_commit_status", AsyncMock()):
            await handle_eol_only_push(
                event, "refs/heads/branch/foo", "abcdef123456", eol_data
            )

            mock_client.republish.assert_awaited_once_with(
                repo="stable",
                app_id="test-repo",
                end_of_life="This application is no longer maintained.",
                end_of_life_rebase=None,
            )


@pytest.mark.asyncio
async def test_handle_eol_only_push_non_production_ref():
    """Test that non-production refs are skipped."""
    from app.routes.webhooks import handle_eol_only_push

    event = WebhookEvent(
        id=uuid.uuid4(),
        source=WebhookSource.GITHUB,
        payload={},
        repository="test-owner/test-repo",
        actor="test-actor",
    )
    eol_data = {"end_of_life": "This application is no longer maintained."}

    mock_client = AsyncMock()
    mock_client.republish = AsyncMock(return_value={"status": "ok"})

    with patch("app.routes.webhooks.FlatManagerClient", return_value=mock_client):
        with patch(
            "app.routes.webhooks.update_commit_status", AsyncMock()
        ) as mock_status:
            await handle_eol_only_push(
                event, "refs/heads/develop", "abcdef123456", eol_data
            )

            mock_client.republish.assert_not_awaited()
            mock_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_flathub_json_http_error():
    """Test that HTTP errors return None."""
    from app.routes.webhooks import fetch_flathub_json

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(side_effect=httpx.HTTPError("Server error"))
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        result = await fetch_flathub_json("test-owner/test-repo", "abc123")

    assert result is None


@pytest.mark.asyncio
async def test_republish_http_error():
    """Test that republish HTTP error sets failure status."""
    from app.routes.webhooks import handle_eol_only_push

    event = WebhookEvent(
        id=uuid.uuid4(),
        source=WebhookSource.GITHUB,
        payload={},
        repository="test-owner/test-repo",
        actor="test-actor",
    )
    eol_data = {"end_of_life": "This application is no longer maintained."}

    mock_client = AsyncMock()
    mock_client.republish = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Internal Server Error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )
    )

    with patch("app.routes.webhooks.FlatManagerClient", return_value=mock_client):
        with patch(
            "app.routes.webhooks.update_commit_status", AsyncMock()
        ) as mock_status:
            await handle_eol_only_push(
                event, "refs/heads/master", "abcdef123456", eol_data
            )

            assert mock_status.await_args_list[0].kwargs["state"] == "pending"
            assert mock_status.await_args_list[1].kwargs["state"] == "failure"
            assert "failed" in mock_status.await_args_list[1].kwargs["description"]


def test_get_eol_only_changes_boolean_value():
    """Test that boolean EOL values return None."""
    from app.routes.webhooks import get_eol_only_changes

    base_json = {"end-of-life": "Old message"}
    head_json = {"end-of-life": True}

    result = get_eol_only_changes(base_json, head_json)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_flathub_json_non_object():
    """Test that non-object JSON returns empty dict."""
    from app.routes.webhooks import fetch_flathub_json

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = Mock(return_value=["array", "instead", "of", "object"])
    mock_response.raise_for_status = Mock()

    mock_client_instance = AsyncMock()
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        result = await fetch_flathub_json("test-owner/test-repo", "abc123")

    assert result == {}
