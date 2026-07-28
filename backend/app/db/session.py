"""Async DB session with graceful fallback.

Preferred: Postgres (docker-compose.dev.yml). If unreachable at startup we
fall back to a local SQLite file so trading state is still durably persisted —
a dev convenience that keeps exit enforcement functional; prod uses Postgres.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = logging.getLogger("app.db")

SQLITE_FALLBACK = "sqlite+aiosqlite:///./trader.db"


class Database:
    def __init__(self):
        self.engine = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.url: str = ""

    async def connect(self, url: str) -> None:
        from app.models.trade import Base

        for candidate in (url, SQLITE_FALLBACK):
            try:
                engine = create_async_engine(candidate, pool_pre_ping=True)
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                self.engine = engine
                self.url = candidate
                self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
                if candidate != url:
                    log.error(
                        "POSTGRES UNAVAILABLE - using SQLite fallback at ./trader.db "
                        "(fine for dev; run docker compose for the real thing)"
                    )
                else:
                    log.info("database connected: %s", candidate.split("@")[-1])
                return
            except Exception as exc:
                log.warning("db connect failed for %s: %s", candidate.split("@")[-1], exc)
        raise RuntimeError("no database available (postgres AND sqlite failed)")

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()

    def session(self) -> AsyncSession:
        if self.session_factory is None:
            raise RuntimeError("database not connected")
        return self.session_factory()
