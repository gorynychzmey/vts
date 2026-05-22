from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vts.core.config import get_settings

settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


def get_db_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async sessionmaker bound to the global engine.

    Suitable for callers that need to open their own session without going
    through the FastAPI Depends graph (e.g. MCP tools).
    """
    return SessionLocal

