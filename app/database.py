from contextlib import asynccontextmanager
from typing import Any
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def get_engine_args() -> dict[str, Any]:
    args: dict[str, Any] = {
        "echo": settings.debug,
        "future": True,
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    return args


writer_engine: AsyncEngine = create_async_engine(
    settings.database_url,
    **get_engine_args(),
)

reader_engine: AsyncEngine = (
    create_async_engine(
        settings.database_replica_url,
        **get_engine_args(),
    )
    if settings.database_replica_url
    else writer_engine
)

engine: AsyncEngine = writer_engine

AsyncWriterSessionLocal = async_sessionmaker(
    bind=writer_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

AsyncReaderSessionLocal = async_sessionmaker(
    bind=reader_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

AsyncSessionLocal = AsyncWriterSessionLocal


@asynccontextmanager
async def get_db(*, use_replica: bool = False) -> AsyncGenerator[AsyncSession]:
    session_factory = (
        AsyncReaderSessionLocal if use_replica else AsyncWriterSessionLocal
    )
    async with session_factory() as session:
        async with session.begin():
            try:
                yield session
            finally:
                await session.close()
