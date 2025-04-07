import pytest
import uuid
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.webhook_event import WebhookEvent, WebhookSource

# Sample GitHub payload (simplified)
SAMPLE_GITHUB_PAYLOAD = {
    "repository": {"full_name": "test-owner/test-repo"},
    "sender": {"login": "test-actor"},
    "action": "opened",
}


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    return mock_session


@pytest.fixture
def mock_db(mock_db_session):
    """Mock the database session factory."""

    class MockAsyncSessionLocal:
        async def __aenter__(self):
            return mock_db_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch(
        "app.routes.webhooks.AsyncSessionLocal", return_value=MockAsyncSessionLocal()
    ):
        yield mock_db_session


@pytest.fixture(scope="function")
def client(mock_db) -> TestClient:
    return TestClient(app)


def test_receive_github_webhook_success(client: TestClient, mock_db_session):
    """Test successful ingestion of a GitHub webhook."""
    delivery_id = str(uuid.uuid4())
    headers = {"X-GitHub-Delivery": delivery_id}

    response = client.post(
        "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
    )

    assert response.status_code == 202
    response_data = response.json()
    assert response_data["message"] == "Webhook received"
    assert response_data["event_id"] == delivery_id

    # Verify that the event was stored correctly
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()
    mock_db_session.refresh.assert_called_once()

    # Get the WebhookEvent that was added to the session
    added_event = mock_db_session.add.call_args[0][0]
    assert isinstance(added_event, WebhookEvent)
    assert str(added_event.id) == delivery_id
    assert added_event.source == WebhookSource.GITHUB
    assert added_event.repository == "test-owner/test-repo"
    assert added_event.actor == "test-actor"
    assert added_event.payload == SAMPLE_GITHUB_PAYLOAD
    # The processed field default is False in the model, but it might be None in tests
    assert added_event.processed is False or added_event.processed is None


def test_receive_github_webhook_missing_header(client: TestClient):
    """Test request without X-GitHub-Delivery header."""
    response = client.post("/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD)
    assert response.status_code == 400
    assert "Missing X-GitHub-Delivery header" in response.text


def test_receive_github_webhook_invalid_header(client: TestClient):
    """Test request with invalid X-GitHub-Delivery header format."""
    headers = {"X-GitHub-Delivery": "not-a-uuid"}
    response = client.post(
        "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
    )
    assert response.status_code == 400
    assert "Invalid X-GitHub-Delivery header format" in response.text


def test_receive_github_webhook_invalid_json(client: TestClient):
    """Test request with invalid JSON payload."""
    delivery_id = str(uuid.uuid4())
    headers = {"X-GitHub-Delivery": delivery_id}
    response = client.post(
        "/api/webhooks/github", content="this is not json", headers=headers
    )
    assert response.status_code == 400
    assert "Invalid JSON payload" in response.text


def test_receive_github_webhook_missing_keys(client: TestClient):
    """Test request with payload missing required keys."""
    delivery_id = str(uuid.uuid4())
    headers = {"X-GitHub-Delivery": delivery_id}
    invalid_payload = {"sender": {"login": "test-actor"}}
    response = client.post(
        "/api/webhooks/github", json=invalid_payload, headers=headers
    )
    assert response.status_code == 422
    assert "Missing expected key" in response.text
    assert "repository" in response.text


def test_receive_github_webhook_nested_key_error(client: TestClient):
    """Test error handling for nested key access."""
    delivery_id = str(uuid.uuid4())
    headers = {"X-GitHub-Delivery": delivery_id}

    # The payload has repository but missing the nested 'full_name' key
    nested_key_error_payload = {
        "repository": {"name": "test-repo"},  # Missing 'full_name'
        "sender": {"login": "test-actor"},
    }

    response = client.post(
        "/api/webhooks/github", json=nested_key_error_payload, headers=headers
    )
    assert response.status_code == 422
    assert "Missing expected key" in response.text


def test_receive_github_webhook_db_commit_error(client: TestClient, mock_db_session):
    """Test handling of database commit errors."""
    # Configure the mock to raise an exception on commit
    mock_db_session.commit.side_effect = Exception("Database commit error")

    delivery_id = str(uuid.uuid4())
    headers = {"X-GitHub-Delivery": delivery_id}

    response = client.post(
        "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
    )

    assert response.status_code == 500
    assert "Database error" in response.text

    # Verify that add was called but commit failed
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()
    mock_db_session.refresh.assert_not_called()


def test_receive_github_webhook_duplicate_event_id(client: TestClient, mock_db_session):
    """Test handling of duplicate event IDs."""
    # Configure the mock to raise an IntegrityError on commit (simulating duplicate key)
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm.exc import MultipleResultsFound

    mock_db_session.commit.side_effect = IntegrityError(
        "Duplicate key", {}, MultipleResultsFound()
    )

    delivery_id = str(uuid.uuid4())
    headers = {"X-GitHub-Delivery": delivery_id}

    response = client.post(
        "/api/webhooks/github", json=SAMPLE_GITHUB_PAYLOAD, headers=headers
    )

    assert response.status_code == 500
    assert "Database error" in response.text

    # Verify that add was called but commit failed
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()
    mock_db_session.refresh.assert_not_called()
