import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.database import get_db, writer_engine, reader_engine


@pytest.mark.asyncio
async def test_replica_engine_uses_writer_when_no_replica_url():
    """Test that reader_engine is the same as writer_engine when no replica URL."""
    assert reader_engine is writer_engine


@pytest.mark.asyncio
async def test_get_db_default_uses_writer():
    """Test that get_db() without parameters uses the writer connection."""
    async with get_db() as session:
        assert session.bind == writer_engine


@pytest.mark.asyncio
async def test_get_db_replica_false_uses_writer():
    """Test that get_db(use_replica=False) explicitly uses the writer connection."""
    async with get_db(use_replica=False) as session:
        assert session.bind == writer_engine


@pytest.mark.asyncio
async def test_get_db_replica_true_without_replica_url():
    """Test that get_db(use_replica=True) falls back to writer when no replica URL."""
    async with get_db(use_replica=True) as session:
        assert session.bind == writer_engine


@pytest.mark.asyncio
async def test_get_db_replica_true_with_replica_url():
    """Test that get_db(use_replica=True) uses reader when replica URL is set."""
    with patch.object(
        settings, "database_replica_url", "postgresql+psycopg://replica:5432/test"
    ):
        # Need to reimport to pick up the new settings
        import app.database

        # Store original engines
        original_writer = app.database.writer_engine
        original_reader = app.database.reader_engine

        try:
            # Mock the engines to avoid actual connection attempts
            mock_writer_engine = AsyncMock(spec=AsyncEngine)
            mock_reader_engine = AsyncMock(spec=AsyncEngine)

            with patch("app.database.writer_engine", mock_writer_engine):
                with patch("app.database.reader_engine", mock_reader_engine):
                    with patch(
                        "app.database.AsyncReaderSessionLocal"
                    ) as mock_reader_factory:
                        with patch(
                            "app.database.AsyncWriterSessionLocal"
                        ) as mock_writer_factory:
                            # Setup mock sessions
                            mock_reader_session = AsyncMock(spec=AsyncSession)
                            mock_reader_session.bind = mock_reader_engine
                            mock_reader_factory.return_value.__aenter__.return_value = (
                                mock_reader_session
                            )
                            mock_reader_factory.return_value.__aexit__.return_value = (
                                None
                            )

                            mock_writer_session = AsyncMock(spec=AsyncSession)
                            mock_writer_session.bind = mock_writer_engine
                            mock_writer_factory.return_value.__aenter__.return_value = (
                                mock_writer_session
                            )
                            mock_writer_factory.return_value.__aexit__.return_value = (
                                None
                            )

                            # Test replica usage
                            async with get_db(use_replica=True):
                                assert mock_reader_factory.called

                            # Test writer usage
                            async with get_db(use_replica=False):
                                assert mock_writer_factory.called
        finally:
            # Restore original engines
            app.database.writer_engine = original_writer
            app.database.reader_engine = original_reader


@pytest.mark.asyncio
async def test_get_db_transaction_isolation():
    """Test that each get_db() call creates a new transaction."""
    sessions = []

    async with get_db() as session1:
        sessions.append(session1)

    async with get_db() as session2:
        sessions.append(session2)

    # Sessions should be different instances
    assert sessions[0] is not sessions[1]
