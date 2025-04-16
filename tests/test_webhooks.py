import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models import Pipeline, PipelineStatus
from app.models.webhook_event import WebhookEvent, WebhookSource

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
SAMPLE_IGNORED_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "closed",
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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db_session

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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db_session

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

    assert should_store_event(SAMPLE_IGNORED_PAYLOAD) is False


def test_receive_github_webhook_ignore_event(client: TestClient, mock_db_session):
    """Test that events not matching criteria are received but not stored."""
    delivery_id = str(uuid.uuid4())
    headers = {
        "X-GitHub-Delivery": delivery_id,
    }

    with patch("app.routes.webhooks.settings.github_webhook_secret", ""):
        response = client.post(
            "/api/webhooks/github", json=SAMPLE_IGNORED_PAYLOAD, headers=headers
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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db_session

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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db_session

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
    comment_payload["comment"]["id"] = 12345
    comment_payload["comment"]["user"] = {"login": "test-user"}
    comment_payload["comment"]["html_url"] = (
        "https://github.com/test-owner/test-repo/pull/42#comment-12345"
    )
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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db_session

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

    @asynccontextmanager
    async def mock_get_db():
        yield mock_db_session

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
