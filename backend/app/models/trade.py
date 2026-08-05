"""Persistence models. TradePlan is the discipline contract: it exists in the
DB *before* the entry order goes to Alpaca, and the exit enforcer is rebuilt
from these rows on every startup — a restart can never orphan a live position.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
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
    # Strategy-runtime provenance: which instance placed this plan (label in
    # `strategy` stays for humans and pre-runtime rows).
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # option: legs are contracts, premiums are per-share x100. equity: one
    # leg of shares, "premium" IS the share price, multiplier 1.
    asset_class: Mapped[str] = mapped_column(String(8), default="option")
    # Equity DAY limits only (verified 2026-08-04: fills premarket, post-
    # market AND overnight — 24/5). Options never set this.
    extended_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    legs: Mapped[list] = mapped_column(JSON)  # [{symbol, right, strike, expiry, side, ratio, entry, iv}]
    qty: Mapped[int] = mapped_column(Integer)  # contract sets / shares

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

    @property
    def contract_multiplier(self) -> int:
        """Dollars per point of premium per unit: 100 for option contract
        sets, 1 for shares. A property derived from asset_class on purpose —
        a separate column could drift from it and nothing needs a third
        value. Default asset_class='option' keeps every pre-equity P/L
        number bit-identical."""
        return 1 if self.asset_class == "equity" else 100

    def pnl_at(self, premium: float | None, qty: int | None = None) -> float | None:
        """Signed P/L dollars marking `qty` units (default: all held) at
        `premium` against the actual entry fill; None when either side of
        the round trip is unknown. THE realized/unrealized P/L formula —
        every P/L number in the system comes from here."""
        if premium is None or self.fill_premium is None:
            return None
        if qty is None:
            qty = self.effective_qty
        return round((premium - self.fill_premium) * self.contract_multiplier * qty, 2)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": as_utc(self.created_at).isoformat() if self.created_at else None,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "strategy_id": self.strategy_id,
            "asset_class": self.asset_class,
            "extended_hours": self.extended_hours,
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
