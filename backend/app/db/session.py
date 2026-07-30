"""Async DB session with graceful fallback.

Preferred: Postgres (docker-compose.dev.yml). If unreachable at startup we
fall back to a local SQLite file so trading state is still durably persisted —
a dev convenience that keeps exit enforcement functional; prod uses Postgres.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = logging.getLogger("app.db")

SQLITE_FALLBACK = "sqlite+aiosqlite:///./trader.db"

# Additive columns introduced after the initial schema. create_all() only
# creates missing TABLES, so pre-existing databases need these ALTERs (a
# lightweight stand-in for full Alembic migrations; both engines support it).
_ADDITIVE_COLUMNS = {
    "trade_plans": {
        "filled_qty": "INTEGER",
        "tp_order_id": "VARCHAR(48)",
        "exited_at": "TIMESTAMP WITH TIME ZONE",
        "exit_fills": "JSON",
    },
}


def _ensure_columns(conn) -> None:
    from sqlalchemy import inspect

    inspector = inspect(conn)
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl_type in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
                log.info("migrated: added %s.%s", table, name)


class Database:
    def __init__(self):
        self.engine = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.url: str = ""

    async def connect(self, url: str) -> None:
        from app.models.trade import Base

        for candidate in (url, SQLITE_FALLBACK):
            try:
                if candidate.startswith("sqlite"):
                    # NullPool: a pooled cap stalls concurrent exit monitors
                    # under load (pool exhaustion = order management hangs).
                    # SQLite connections are cheap; WAL + busy timeout make
                    # concurrent readers/writer behave.
                    from sqlalchemy.pool import NullPool

                    engine = create_async_engine(
                        candidate, poolclass=NullPool, connect_args={"timeout": 30}
                    )

                    from sqlalchemy import event

                    @event.listens_for(engine.sync_engine, "connect")
                    def _sqlite_pragmas(dbapi_conn, _record):
                        cursor = dbapi_conn.cursor()
                        cursor.execute("PRAGMA journal_mode=WAL")
                        cursor.execute("PRAGMA synchronous=NORMAL")
                        cursor.close()
                else:
                    engine = create_async_engine(
                        candidate, pool_pre_ping=True,
                        pool_size=10, max_overflow=20, pool_timeout=10,
                    )
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                    await conn.run_sync(_ensure_columns)
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
