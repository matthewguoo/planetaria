"""Signal journal access. Journal-before-publish is the rule: an event that
reached a strategy but not the journal would be an unexplainable trade.

Timer ticks are the one exception (JOURNALED_TYPES): they carry no outside
information — replaying one is meaningless and journaling every tick would
bloat the table by thousands of rows a day for nothing.
"""

import logging
from dataclasses import replace

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.signals import SignalRow
from app.models.trade import utcnow

from .events import Event

log = logging.getLogger("app.signals")

JOURNALED_TYPES = frozenset({"news", "estimate", "manual", "signal"})


class SignalStore:
    def __init__(self, db):
        self.db = db

    async def record(self, event: Event) -> tuple[Event, bool]:
        """Journal the event. Returns (event-with-signal_id, created).
        created=False means the (source, key) pair was already journaled —
        a feed-reconnect replay the caller must NOT re-publish."""
        from app.services.call_log import CALL_LOG

        if event.type not in JOURNALED_TYPES:
            return event, True
        CALL_LOG.record("engine", f"journal:{event.type}",
                        detail=f"{event.source} {event.key or ''}".strip())
        row = SignalRow(
            ts=event.ts,
            source=event.source,
            type=event.type,
            key=event.key,
            symbols=list(event.symbols),
            payload=event.payload,
        )
        async with self.db.session() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(SignalRow.id).where(
                        SignalRow.source == event.source, SignalRow.key == event.key
                    )
                )
                return replace(event, signal_id=existing), False
            return replace(event, signal_id=row.id), True

    async def record_analysis(self, event: Event) -> Event:
        """Journal an un-prompted (root) analysis row. Analyses are unkeyed —
        every call is a distinct observation, so no dedupe applies."""
        row = SignalRow(
            ts=event.ts, source=event.source, type=event.type, key=None,
            symbols=list(event.symbols), payload=event.payload,
        )
        async with self.db.session() as session:
            session.add(row)
            await session.commit()
            return replace(event, signal_id=row.id)

    async def enrich(self, parent_id: int, event: Event) -> Event:
        """Journal a derived row chained to its source (the LLM analyst)."""
        row = SignalRow(
            ts=event.ts, source=event.source, type=event.type, key=event.key,
            symbols=list(event.symbols), payload=event.payload, parent_id=parent_id,
        )
        async with self.db.session() as session:
            session.add(row)
            await session.commit()
            return replace(event, signal_id=row.id)

    async def recent(
        self, limit: int = 100, type: str | None = None, symbol: str | None = None
    ) -> list[dict]:
        query = select(SignalRow).order_by(SignalRow.id.desc()).limit(min(limit, 500))
        if type:
            query = query.where(SignalRow.type == type)
        async with self.db.session() as session:
            rows = (await session.scalars(query)).all()
        out = [r.to_dict() for r in rows]
        if symbol:  # JSON containment differs per dialect; filter in code
            out = [r for r in out if symbol in (r["symbols"] or [])]
        return out

    async def by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        async with self.db.session() as session:
            rows = (await session.scalars(
                select(SignalRow).where(SignalRow.id.in_(ids))
            )).all()
        return [r.to_dict() for r in rows]

    async def prune(self, keep_days: int = 90) -> int:
        """Retention: the journal is for replay/debug, not eternity.
        plan_events (the trade audit trail) is elsewhere and never pruned."""
        from datetime import timedelta

        cutoff = utcnow() - timedelta(days=keep_days)
        async with self.db.session() as session:
            result = await session.execute(delete(SignalRow).where(SignalRow.ts < cutoff))
            await session.commit()
        if result.rowcount:
            log.info("pruned %d signal rows older than %dd", result.rowcount, keep_days)
        return result.rowcount or 0
