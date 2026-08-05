"""Signal journal: every decision-relevant event that entered the engine
(news, estimates, manual triggers), journaled BEFORE it is published to the
in-process bus. The replay chain for "why did this trade happen" starts here:
signals -> strategy_decisions -> trade_plans/plan_events.

parent_id chains enrichment: a future LLM worker reads raw rows and writes
its structured output as a child row, so strategies can consume enriched
signals with full provenance and nothing is ever overwritten.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.trade import Base


class SignalRow(Base):
    __tablename__ = "signals"
    __table_args__ = (
        # Feed reconnects replay recent items; the journal must not double up.
        # NULL keys (unkeyed event types) are exempt on both dialects.
        Index("ux_signals_source_key", "source", "key", unique=True),
        Index("ix_signals_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(48))   # adapter name
    type: Mapped[str] = mapped_column(String(24))     # news | estimate | manual | signal
    key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbols: Mapped[list | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )

    def to_dict(self) -> dict:
        from app.models.trade import as_utc

        return {
            "id": self.id,
            "ts": as_utc(self.ts).isoformat() if self.ts else None,
            "source": self.source,
            "type": self.type,
            "key": self.key,
            "symbols": self.symbols,
            "payload": self.payload,
            "parent_id": self.parent_id,
        }
