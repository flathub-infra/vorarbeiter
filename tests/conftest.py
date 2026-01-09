import pytest
import os
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
import pytest_asyncio
from contextlib import asynccontextmanager

# Force SQLite for tests
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.main import app
from app.config import settings
from app.database import engine as app_engine
from app.models import Base
import app.utils.github as github_module


@pytest.fixture(scope="session", autouse=True)
def override_settings():
    with patch.object(settings, "database_url", "sqlite+aiosqlite:///:memory:"):
        with patch.object(settings, "flat_manager_url", "https://hub.flathub.org"):
            with patch.object(
                settings, "flat_manager_token", "test_flat_manager_token"
            ):
                yield


@pytest.fixture(autouse=True)
def reset_github_clients():
    github_module._github_client = None
    github_module._github_actions_client = None
    yield
    github_module._github_client = None
    github_module._github_actions_client = None


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.flush = AsyncMock()
    return mock_session


@pytest_asyncio.fixture(scope="function")
async def _real_db_session_generator():
    """(Internal) Provide a session maker after setting up/tearing down the DB."""
    async with app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=app_engine, expire_on_commit=False)

    try:
        yield session_maker
    finally:
        async with app_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="function")
def db_session_maker(_real_db_session_generator):
    return _real_db_session_generator


@pytest.fixture
def client():
    test_client = TestClient(app)
    yield test_client


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer raeVenga1eez3Geeca"}


def create_mock_get_db(mock_session):
    """Create a mock get_db function that accepts use_replica parameter."""

    @asynccontextmanager
    async def mock_get_db(*, use_replica: bool = False):
        yield mock_session

    return mock_get_db
