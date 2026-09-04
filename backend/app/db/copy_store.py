"""Copy one planetaria store into another, table by table, through the
app's own models - the SQLite paper store on the Windows box into the
Linux box's Postgres, in practice.

    python -m app.db.copy_store --source sqlite+aiosqlite:///.../trader.db \\
        --target postgresql+asyncpg://trader:trader@localhost:5432/trader [--dry-run|--verify|--force]

Rules that make this safe to point at a real store:
- the target is opened with fallback=False: a mistyped URL raises instead of
  "succeeding" into a fresh ./trader.db;
- the target schema comes from the app's own fresh-DB path (create_all +
  stamp head), so it is exactly what the migration-parity test guarantees;
- the source must be stamped at the current head - a schema-drifted store
  is refused rather than half-copied;
- a non-empty target is refused unless --force (which wipes it first);
- rows are copied in FK order with primary keys preserved, naive datetimes
  become UTC-aware (the app's as_utc contract), and Postgres sequences are
  reset afterwards so the next insert does not collide.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, Table, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import Database, alembic_config

log = logging.getLogger("app.copy_store")

BATCH = 500


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config("sqlite+aiosqlite://")).get_current_head()


def tables_in_order() -> list[Table]:
    """Every model table, parents before children."""
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.models.trade import Base

    return list(Base.metadata.sorted_tables)


def normalize(table: Table, row: dict) -> dict:
    """SQLite hands naive datetimes back; the app stores UTC, and asyncpg
    refuses a naive value for timestamptz. Everything else passes through
    (JSON columns are (de)serialized by Core on both dialects)."""
    out = dict(row)
    for col in table.columns:
        if isinstance(col.type, DateTime):
            value = out.get(col.name)
            if isinstance(value, datetime) and value.tzinfo is None:
                out[col.name] = value.replace(tzinfo=timezone.utc)
    return out


def integer_pk_tables(tables: list[Table]) -> list[Table]:
    return [
        t for t in tables
        if len(t.primary_key.columns) == 1
        and isinstance(list(t.primary_key.columns)[0].type, Integer)
    ]


def _raw_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


async def stamped_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        def _rev(sync_conn):
            from sqlalchemy import inspect

            if "alembic_version" not in inspect(sync_conn).get_table_names():
                return None
            return sync_conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

        return await conn.run_sync(_rev)


async def table_counts(engine: AsyncEngine, tables: list[Table]) -> dict[str, int | None]:
    """Row count per table; None when the table does not exist."""
    counts: dict[str, int | None] = {}
    async with engine.connect() as conn:
        def _existing(sync_conn):
            from sqlalchemy import inspect

            return set(inspect(sync_conn).get_table_names())

        existing = await conn.run_sync(_existing)
        for t in tables:
            if t.name not in existing:
                counts[t.name] = None
                continue
            counts[t.name] = (await conn.execute(select(func.count()).select_from(t))).scalar_one()
    return counts


async def copy_table(source: AsyncEngine, target: AsyncEngine, table: Table) -> int:
    pk = list(table.primary_key.columns)
    order = pk if pk else list(table.columns)
    copied = 0
    async with source.connect() as src, target.begin() as dst:
        offset = 0
        while True:
            rows = (await src.execute(
                select(table).order_by(*order).limit(BATCH).offset(offset)
            )).mappings().all()
            if not rows:
                break
            await dst.execute(insert(table), [normalize(table, dict(r)) for r in rows])
            copied += len(rows)
            offset += BATCH
    return copied


async def reset_sequences(target: AsyncEngine, tables: list[Table]) -> dict[str, int]:
    """Postgres: after inserting explicit ids the serial sequence still
    points at 1; the first new row would collide. setval to max(id)."""
    if target.dialect.name != "postgresql":
        return {}
    out: dict[str, int] = {}
    async with target.begin() as conn:
        for t in integer_pk_tables(tables):
            col = list(t.primary_key.columns)[0].name
            seq = (await conn.execute(text(f"SELECT pg_get_serial_sequence('{t.name}', '{col}')"))).scalar()
            if not seq:
                continue
            max_id = (await conn.execute(text(f"SELECT MAX({col}) FROM {t.name}"))).scalar()
            if max_id is None:
                await conn.execute(text(f"SELECT setval('{seq}', 1, false)"))
                out[t.name] = 0
            else:
                await conn.execute(text(f"SELECT setval('{seq}', {int(max_id)}, true)"))
                out[t.name] = int(max_id)
    return out


async def wipe(target: AsyncEngine, tables: list[Table]) -> None:
    async with target.begin() as conn:
        for t in reversed(tables):
            await conn.execute(t.delete())


async def run(source_url: str, target_url: str, *, dry_run: bool, verify: bool, force: bool) -> int:
    tables = tables_in_order()
    head = head_revision()
    source = _raw_engine(source_url)
    try:
        src_rev = await stamped_revision(source)
        if src_rev != head:
            log.error("source is stamped at %r, head is %r - refusing (migrate it first)", src_rev, head)
            return 2
        src_counts = await table_counts(source, tables)
        log.info("source %s: %s", source_url.split("@")[-1], src_counts)

        if dry_run:
            probe = _raw_engine(target_url)
            try:
                tgt_counts = await table_counts(probe, tables)
                log.info("target %s: %s (rev %s)", target_url.split("@")[-1], tgt_counts,
                         await stamped_revision(probe))
            finally:
                await probe.dispose()
            log.info("dry-run: nothing written")
            return 0

        db = Database()
        await db.connect(target_url, fallback=False)  # creates + stamps a fresh schema
        target = db.engine
        try:
            tgt_counts = await table_counts(target, tables)
            if verify:
                return await _verify(source, target, tables, src_counts, tgt_counts, head)
            populated = {k: v for k, v in tgt_counts.items() if v}
            if populated and not force:
                log.error("target is not empty %s - refusing without --force", populated)
                return 3
            if populated:
                log.warning("--force: wiping target %s", populated)
                await wipe(target, tables)
            for t in tables:
                n = await copy_table(source, target, t)
                log.info("copied %-20s %6d rows", t.name, n)
            seqs = await reset_sequences(target, tables)
            if seqs:
                log.info("sequences reset: %s", seqs)
            return await _verify(source, target, tables, src_counts,
                                 await table_counts(target, tables), head)
        finally:
            await db.close()
    finally:
        await source.dispose()


async def _verify(source, target, tables, src_counts, tgt_counts, head) -> int:
    ok = True
    for t in tables:
        if src_counts[t.name] != tgt_counts[t.name]:
            log.error("count mismatch %s: source %s target %s", t.name, src_counts[t.name], tgt_counts[t.name])
            ok = False
    tgt_rev = await stamped_revision(target)
    if tgt_rev != head:
        log.error("target stamped at %r, head is %r", tgt_rev, head)
        ok = False
    if target.dialect.name == "postgresql":
        async with target.connect() as conn:
            for t in integer_pk_tables(tables):
                col = list(t.primary_key.columns)[0].name
                seq = (await conn.execute(text(f"SELECT pg_get_serial_sequence('{t.name}', '{col}')"))).scalar()
                if not seq:
                    continue
                last, called = (await conn.execute(text(f"SELECT last_value, is_called FROM {seq}"))).one()
                max_id = (await conn.execute(text(f"SELECT COALESCE(MAX({col}), 0) FROM {t.name}"))).scalar()
                if max_id and (last != max_id or not called):
                    log.error("sequence %s at %s/%s but max(%s)=%s", seq, last, called, col, max_id)
                    ok = False
    log.info("verify: %s (counts %s, rev %s)", "OK" if ok else "FAILED", tgt_counts, tgt_rev)
    return 0 if ok else 4


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report counts on both sides, write nothing")
    mode.add_argument("--verify", action="store_true", help="compare an already-copied target")
    mode.add_argument("--force", action="store_true", help="wipe a non-empty target first")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.source, args.target, dry_run=args.dry_run,
                           verify=args.verify, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
