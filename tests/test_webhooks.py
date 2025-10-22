import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models import Pipeline, PipelineStatus
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
            "ref": "main",
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
            with patch("app.routes.webhooks.create_pipeline", return_value=None):
                response = client.post(
                    "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
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


def test_should_not_store_event():
    """Test should_store_event returns False for other events."""
    from app.routes.webhooks import should_store_event

    payloads = [
        SAMPLE_IGNORED_PAYLOAD_1,
        SAMPLE_IGNORED_PAYLOAD_2,
        SAMPLE_IGNORED_PAYLOAD_3,
        SAMPLE_IGNORED_PAYLOAD_4,
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
            with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
                response = client.post(
                    "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
                )

                assert response.status_code == 202
                response_data = response.json()
                assert response_data["message"] == "Webhook received"
                assert response_data["event_id"] == delivery_id
                assert response_data["pipeline_id"] == str(pipeline_id)

                assert mock_db_session.add.called
                assert mock_db_session.commit.called


# Test data for retry functionality
SAMPLE_ISSUE_BODY_STABLE = """The stable build pipeline for `test-app` failed.

Commit SHA: abc123456789
Build log: https://example.com/log/123"""

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


def test_parse_failure_issue_stable_build():
    from app.routes.webhooks import parse_failure_issue

    result = parse_failure_issue(SAMPLE_ISSUE_BODY_STABLE, "flathub/test-app")

    assert result is not None
    assert result["sha"] == "abc123456789"
    assert result["repo"] == "flathub/test-app"
    assert result["ref"] == "refs/heads/master"
    assert result["flat_manager_repo"] == "stable"
    assert result["issue_type"] == "build_failure"


def test_parse_failure_issue_job_failure():
    from app.routes.webhooks import parse_failure_issue

    result = parse_failure_issue(SAMPLE_ISSUE_BODY_JOB_FAILURE, "flathub/test-app")

    assert result is not None
    assert result["sha"] == "abc123456789"
    assert result["repo"] == "flathub/test-app"
    assert result["ref"] == "refs/heads/master"
    assert result["flat_manager_repo"] == "stable"
    assert result["issue_type"] == "job_failure"
    assert result["job_type"] == "commit"


def test_parse_failure_issue_invalid():
    from app.routes.webhooks import parse_failure_issue

    invalid_body = "This is not a build failure issue."
    result = parse_failure_issue(invalid_body, "flathub/test-app")

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

    with patch("app.routes.webhooks.validate_retry_permissions", return_value=True):
        with patch(
            "app.routes.webhooks.BuildPipeline", return_value=mock_pipeline_service
        ):
            with patch("app.routes.webhooks.update_commit_status", AsyncMock()):
                with patch("app.routes.webhooks.add_issue_comment", AsyncMock()):
                    with patch("app.routes.webhooks.close_github_issue", AsyncMock()):
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
            "app.routes.webhooks.get_issue_details",
            AsyncMock(return_value={"user": {"login": "flathubbot"}}),
        ),
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
            "app.routes.webhooks.get_issue_details",
            AsyncMock(return_value={"user": {"login": "flathubbot"}}),
        ),
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
