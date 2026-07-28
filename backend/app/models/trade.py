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


# status lifecycle:
#   planned -> submitted -> filled -> exiting -> closed
#                      \-> cancelled (entry never filled)
#                      \-> rejected
TERMINAL_STATUSES = {"closed", "cancelled", "rejected"}
OPEN_STATUSES = {"planned", "submitted", "filled", "exiting"}


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
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)  # tp|sl|time_stop|manual|flatten
    fill_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

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
            "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id,
            "exit_reason": self.exit_reason,
            "fill_premium": self.fill_premium,
            "exit_premium": self.exit_premium,
            "realized_pnl": self.realized_pnl,
        }


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
