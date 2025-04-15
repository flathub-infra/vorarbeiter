import pytest
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

# Force SQLite for tests
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.main import app
from app.config import settings
from app.database import get_db


@pytest.fixture(scope="session", autouse=True)
def override_settings():
    """Override settings for testing."""
    with patch.object(settings, "database_url", "sqlite+aiosqlite:///:memory:"):
        with patch.object(settings, "flat_manager_url", "https://hub.flathub.org"):
            with patch.object(
                settings, "flat_manager_token", "test_flat_manager_token"
            ):
                yield


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.flush = AsyncMock()
    return mock_session


@pytest.fixture
def client(mock_db_session):
    """Create a test client with a mocked database session."""

    @asynccontextmanager
    async def override_get_db():
        try:
            yield mock_db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
