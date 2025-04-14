from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def get_engine_args() -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "echo": settings.debug,
        "future": True,
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    return args


engine: AsyncEngine = create_async_engine(
    settings.database_url,
    **get_engine_args(),
)


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Context manager that provides an AsyncSession."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            try:
                yield session
            finally:
                await session.close()
