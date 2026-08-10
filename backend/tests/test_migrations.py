"""Alembic adoption: the three connect paths (fresh / pre-Alembic / stamped)
and the parity guarantee that create_all+stamp is indistinguishable from
walking the migration chain."""

import asyncio
import sqlite3

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import Database, _run_alembic, alembic_config


def _head_revision() -> str:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("sqlite+aiosqlite://"))
    return script.get_current_head()


async def _survey(url: str) -> tuple[set[tuple[str, str, str, bool]], str | None]:
    """(table, column, type, nullable) tuples (minus alembic_version) +
    stamped revision. Type and nullability matter: 0007 was exactly a
    nullability migration, the case a name-only comparison cannot see."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            def _collect(sync_conn):
                inspector = inspect(sync_conn)
                pairs = {
                    (t, col["name"], str(col["type"]), bool(col["nullable"]))
                    for t in inspector.get_table_names()
                    if t != "alembic_version"
                    for col in inspector.get_columns(t)
                }
                rev = None
                if "alembic_version" in inspector.get_table_names():
                    rev = sync_conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar()
                return pairs, rev

            return await conn.run_sync(_collect)
    finally:
        await engine.dispose()


# Pre-Alembic schema: trade_plans WITHOUT the five legacy-additive columns,
# no alembic_version. This is what a store from before commit 6a228e1-era
# looks like.
_OLD_SCHEMA = """
CREATE TABLE trade_plans (
    id VARCHAR(32) PRIMARY KEY, created_at TIMESTAMP, updated_at TIMESTAMP,
    underlying VARCHAR(12) NOT NULL, strategy VARCHAR(24) NOT NULL,
    legs JSON NOT NULL, qty INTEGER NOT NULL,
    entry_limit FLOAT NOT NULL, tp_premium FLOAT NOT NULL,
    sl_premium FLOAT NOT NULL, time_stop_utc TIMESTAMP NOT NULL,
    status VARCHAR(16) NOT NULL, entry_order_id VARCHAR(64),
    exit_order_id VARCHAR(64), exit_reason VARCHAR(24),
    fill_premium FLOAT, exit_premium FLOAT, realized_pnl FLOAT, notes TEXT
);
CREATE INDEX ix_trade_plans_status ON trade_plans (status);
CREATE TABLE app_settings (
    key VARCHAR(48) PRIMARY KEY, value JSON NOT NULL, updated_at TIMESTAMP
);
CREATE TABLE plan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TIMESTAMP NOT NULL,
    plan_id VARCHAR(32) NOT NULL, event VARCHAR(32) NOT NULL,
    source_status VARCHAR(24) NOT NULL, target_status VARCHAR(24),
    applied INTEGER NOT NULL, detail TEXT
);
"""


@pytest.mark.asyncio
async def test_fresh_db_created_and_stamped_at_head(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/fresh.db"
    db = Database()
    await db.connect(url)
    await db.close()

    pairs, rev = await _survey(url)
    tables = {t for t, *_ in pairs}
    assert {"trade_plans", "app_settings", "plan_events"} <= tables
    assert rev == _head_revision()


@pytest.mark.asyncio
async def test_pre_alembic_db_backfilled_stamped_upgraded(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.close()

    url = f"sqlite+aiosqlite:///{path}"
    db = Database()
    await db.connect(url)
    await db.close()

    pairs, rev = await _survey(url)
    # The five legacy-additive columns arrived via the one-time backfill.
    present = {(t, c) for t, c, *_ in pairs}
    for col in ("filled_qty", "tp_order_id", "exited_at", "exit_fills", "exec_quality"):
        assert ("trade_plans", col) in present, col
    assert rev == _head_revision()


@pytest.mark.asyncio
async def test_chain_parity_with_metadata(tmp_path):
    """Walking the migration chain from empty must produce exactly the schema
    create_all produces — the guard that lets fresh DBs take the fast path."""
    chain_url = f"sqlite+aiosqlite:///{tmp_path}/chain.db"
    await asyncio.to_thread(_run_alembic, chain_url, "upgrade", "head")
    chain_pairs, _ = await _survey(chain_url)

    fresh_url = f"sqlite+aiosqlite:///{tmp_path}/fresh.db"
    db = Database()
    await db.connect(fresh_url)
    await db.close()
    fresh_pairs, _ = await _survey(fresh_url)

    assert chain_pairs == fresh_pairs


@pytest.mark.asyncio
async def test_reconnect_is_idempotent(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/twice.db"
    for _ in range(2):
        db = Database()
        await db.connect(url)
        await db.close()
    _, rev = await _survey(url)
    assert rev == _head_revision()
