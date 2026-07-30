"""Persistence models. TradePlan is the discipline contract: it exists in the
DB *before* the entry order goes to Alpaca, and the exit enforcer is rebuilt
from these rows on every startup — a restart can never orphan a live position.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite round-trips DateTime(timezone=True) as offset-naive; values are
    stored in UTC, so re-attach tzinfo before any aware comparison."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# Status lifecycle is governed by app.services.plan_fsm (FIX-style explicit
# transition table); these string sets mirror its state groups for queries.
#   planned -> submitted -> [partially_filled ->] filled -> exiting -> closed
#                      \-> cancelled (entry never filled)
#                      \-> rejected
TERMINAL_STATUSES = {"closed", "cancelled", "rejected"}
OPEN_STATUSES = {"planned", "submitted", "partially_filled", "filled", "exiting"}


class TradePlan(Base):
    __tablename__ = "trade_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    underlying: Mapped[str] = mapped_column(String(12))
    strategy: Mapped[str] = mapped_column(String(24))  # long_call, put_spread, ...
    legs: Mapped[list] = mapped_column(JSON)  # [{symbol, right, strike, expiry, side, ratio, entry, iv}]
    qty: Mapped[int] = mapped_column(Integer)  # contract sets

    entry_limit: Mapped[float] = mapped_column(Float)   # net debit limit / share
    tp_premium: Mapped[float] = mapped_column(Float)    # position value / share
    sl_premium: Mapped[float] = mapped_column(Float)
    time_stop_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    filled_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)  # partial-fill tracking
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Resting take-profit limit order held AT THE BROKER while the plan is
    # filled: zero-latency TP fills that survive engine downtime. SL/time
    # remain software-enforced (no broker stop orders for options).
    tp_order_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)  # tp|sl|time_stop|manual|flatten|external
    fill_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ACTUAL closing-fill time at the broker. The lifecycle journal records
    # when the ENGINE learned of the close — hours later for an external
    # liquidation captured by reconcile — so the chart's exit marker needs
    # the broker's own timestamp, not ours.
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # External liquidations land in CHUNKS at different times and prices
    # (broker auto-liquidate, expiry sweeps). Each event: {ts (ISO), premium
    # (net per set), qty (sets)} — the chart draws one exit marker per event;
    # exit_premium/exited_at above stay the qty-weighted total / last fill.
    exit_fills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Execution-quality ledger: {entry|exit: {fair, half_spread, ts, fill,
    # cost, spread_capture}}. fair = net microprice at SUBMIT; cost = signed
    # premium given up vs fair (per set); spread_capture = 1 - cost/half_spread
    # (1.0 = filled at fair, 0.0 = crossed the whole half-spread). The
    # literature's verdict is that spread friction — not direction — is where
    # retail options P/L dies; this measures ours on every fill.
    exec_quality: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def effective_qty(self) -> int:
        """Contract sets actually held (partial fills); falls back to plan qty."""
        return self.filled_qty if self.filled_qty else self.qty

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": as_utc(self.created_at).isoformat() if self.created_at else None,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "legs": self.legs,
            "qty": self.qty,
            "entry_limit": self.entry_limit,
            "tp_premium": self.tp_premium,
            "sl_premium": self.sl_premium,
            "time_stop_utc": as_utc(self.time_stop_utc).isoformat() if self.time_stop_utc else None,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id,
            "tp_order_id": self.tp_order_id,
            "exit_reason": self.exit_reason,
            "fill_premium": self.fill_premium,
            "exit_premium": self.exit_premium,
            "realized_pnl": self.realized_pnl,
            "exited_at": as_utc(self.exited_at).isoformat() if self.exited_at else None,
            "exit_fills": self.exit_fills,
            "exec_quality": self.exec_quality,
        }


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PlanEventRow(Base):
    """Append-only journal of every lifecycle event that reached the FSM —
    including DROPPED ones (illegal in state, lost CAS, failed guard). The
    audit trail for "why did this position do that": nothing mutates a plan
    without a row here, and rows are never updated or deleted."""

    __tablename__ = "plan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    plan_id: Mapped[str] = mapped_column(String(32), index=True)
    event: Mapped[str] = mapped_column(String(32))
    source_status: Mapped[str] = mapped_column(String(24))
    target_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    applied: Mapped[int] = mapped_column(Integer)  # 1 applied / 0 dropped
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat() if self.ts else None,
            "event": self.event,
            "source": self.source_status,
            "target": self.target_status,
            "applied": bool(self.applied),
            "detail": self.detail,
        }
