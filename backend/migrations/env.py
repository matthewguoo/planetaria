"""Alembic environment. Works in three invocation modes:

1. Programmatic (app.db.session): Config built in code, resolved async URL in
   config.attributes["sqlalchemy_url"] (attributes, not main options — main
   options pass through configparser interpolation, which chokes on % in
   passwords).
2. CLI with explicit target: alembic upgrade head -x db_url=<async url>
3. Bare CLI: falls back to app Settings().database_url (NOT get_settings() —
   the paper-lock validation has no business gating a schema migration).

Both dialects the app supports (postgresql+asyncpg, sqlite+aiosqlite) are
async, so migrations always run through an AsyncEngine; render_as_batch is
enabled on SQLite because it cannot ALTER in place.
"""

import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.models.trade import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def _resolve_url() -> str:
    url = config.attributes.get("sqlalchemy_url")
    if url:
        return url
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    xargs = context.get_x_argument(as_dictionary=True)
    if xargs.get("db_url"):
        return xargs["db_url"]
    from app.config import Settings

    return Settings().database_url


def _configure(connection=None, url=None) -> None:
    is_sqlite = (url or str(connection.engine.url)).startswith("sqlite")
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        render_as_batch=is_sqlite,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    _configure(url=_resolve_url())
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations(_resolve_url()))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
