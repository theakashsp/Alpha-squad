"""
Async SQLAlchemy engine wired to Neon (PostgreSQL + PostGIS).

We use asyncpg as the driver and a *pooled* Neon connection string so that
every serverless/edge invocation reuses a pre-warmed connection.
"""
from collections.abc import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from backend.config import settings

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# asyncpg does not accept sslmode= or channel_binding= in the query string.
# Strip both, then pass ssl="require" via connect_args.
import re as _re

def _clean_url(raw: str) -> str:
    # Remove sslmode=require, channel_binding=require, and trailing ?&
    cleaned = _re.sub(r"[?&](sslmode|channel_binding)=[^&]*", "", raw)
    cleaned = cleaned.rstrip("?&")
    return cleaned

_raw_url: str = _clean_url(settings.neon_database_url)

engine: AsyncEngine = create_async_engine(
    _raw_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    connect_args={
        "statement_cache_size": 0,   # required by Neon pooler
        "ssl": "require",            # asyncpg uses ssl=, not sslmode=
    },
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Dependency – FastAPI route injection
# ---------------------------------------------------------------------------
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Schema / table creation (called once at startup via lifespan)
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """Create all tables and enable PostGIS extension if not present."""
    async with engine.begin() as conn:
        # Enable PostGIS (idempotent)
        await conn.execute(
            __import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS postgis;")
        )
        # Create tables defined via SQLModel metadata
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("Database initialised (PostGIS enabled, tables synced).")


async def close_db() -> None:
    await engine.dispose()
    logger.info("Database engine disposed.")
