"""Async DB session with graceful fallback.

Preferred: Postgres (docker-compose.dev.yml). If unreachable at startup we
fall back to a local SQLite file so trading state is still durably persisted —
a dev convenience that keeps exit enforcement functional; prod uses Postgres.

Schema is owned by the Alembic chain in backend/migrations. On connect:
  - fresh DB            -> create_all + stamp head (fast path; the parity
                           test in tests/test_migrations.py guarantees this
                           is identical to walking the chain)
  - pre-Alembic DB      -> one last legacy column backfill, stamp baseline,
                           upgrade head
  - already-stamped DB  -> upgrade head
The Postgres and SQLite-fallback stores each carry their own alembic_version
and migrate independently — matching their existing divergence-by-design.
"""

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = logging.getLogger("app.db")

SQLITE_FALLBACK = "sqlite+aiosqlite:///./trader.db"

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_BASELINE_REV = "0001"

# Columns the retired pre-Alembic shim used to add. Kept ONLY for the one-time
# stamp path: a DB created before Alembic adoption may predate these, and the
# baseline revision assumes they exist.
_LEGACY_ADDITIVE_COLUMNS = {
    "trade_plans": {
        "filled_qty": "INTEGER",
        "tp_order_id": "VARCHAR(48)",
        "exited_at": "TIMESTAMP WITH TIME ZONE",
        "exit_fills": "JSON",
        "exec_quality": "JSON",
    },
}


def _ensure_legacy_columns(conn) -> None:
    from sqlalchemy import inspect

    inspector = inspect(conn)
    for table, columns in _LEGACY_ADDITIVE_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl_type in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
                log.info("legacy backfill: added %s.%s", table, name)


def alembic_config(url: str):
    """Programmatic Config; the URL travels via attributes (never main
    options — configparser interpolation mangles % in passwords)."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


def _run_alembic(url: str, action: str, revision: str) -> None:
    """Sync alembic command entry point — call via asyncio.to_thread. env.py
    runs asyncio.run() internally, so this must never execute on a thread
    that already owns an event loop."""
    from alembic import command

    cfg = alembic_config(url)
    getattr(command, action)(cfg, revision)


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
                await self._migrate(engine, candidate, Base)
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

    async def _migrate(self, engine, url: str, Base) -> None:
        """Bring the store to schema head (see module docstring for the
        three-way branch). Inspection runs in its own transaction, which is
        CLOSED before alembic opens its own engine — a held write txn would
        deadlock SQLite."""
        from sqlalchemy import inspect

        def _survey(conn) -> tuple[bool, bool]:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            return "trade_plans" in tables, "alembic_version" in tables

        async with engine.begin() as conn:
            has_schema, has_version = await conn.run_sync(_survey)
            if not has_schema and not has_version:
                await conn.run_sync(Base.metadata.create_all)
            elif has_schema and not has_version:
                await conn.run_sync(_ensure_legacy_columns)

        if not has_schema and not has_version:
            await asyncio.to_thread(_run_alembic, url, "stamp", "head")
            log.info("fresh schema created and stamped at head")
        elif has_schema and not has_version:
            await asyncio.to_thread(_run_alembic, url, "stamp", _BASELINE_REV)
            await asyncio.to_thread(_run_alembic, url, "upgrade", "head")
            log.info("pre-Alembic store stamped at %s and upgraded to head", _BASELINE_REV)
        else:
            await asyncio.to_thread(_run_alembic, url, "upgrade", "head")

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()

    def session(self) -> AsyncSession:
        if self.session_factory is None:
            raise RuntimeError("database not connected")
        return self.session_factory()
