"""Strategy runtime persistence.

strategy_instances is the server-side registry: which strategies exist, with
what params, in what state. The DB row is the source of truth — the runner
reconstructs its tasks from these rows at startup exactly like the exit
enforcer rebuilds monitors from trade_plans.

strategy_decisions is the decision journal: every intent a strategy emitted
and what happened to it (placed / rejected / skipped / errored), with
provenance into the signals table. Combined with trade_plans + plan_events,
any automated trade is reconstructible from persisted rows alone.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.trade import Base, as_utc, utcnow

STRATEGY_STATES = {"disabled", "enabled", "paused"}
DECISION_ACTIONS = {"intent", "placed", "rejected", "skip", "error", "note"}

# How much of the account an instance may deploy. `pct` is of current account
# equity and so moves with the account; `usd` is a fixed dollar ceiling that
# does not. Default 10% rather than 100%: a strategy that has never traded
# should not inherit the whole account by omission.
ALLOCATION_MODES = {"pct", "usd"}
DEFAULT_ALLOCATION = {"mode": "pct", "value": 10.0}


# The per-strategy circuit breaker. Replaces the per-position stop as the
# thing that bounds a strategy's loss: stops protect one trade, a breaker
# protects the strategy, which is the unit capital is actually allocated to.
# `pct` is of the strategy's allocation, so a breaker does not silently loosen
# when the account grows. None (disabled) is allowed and is what an
# unbracketed strategy should NOT be run with.
DEFAULT_BREAKER = {"enabled": True, "mode": "pct", "value": 20.0}


def validate_breaker(breaker: dict | None) -> dict:
    if breaker is None:
        return dict(DEFAULT_BREAKER)
    enabled = bool(breaker.get("enabled", True))
    mode = str(breaker.get("mode", "pct"))
    if mode not in ALLOCATION_MODES:
        raise ValueError(f"breaker mode must be one of {sorted(ALLOCATION_MODES)}")
    try:
        value = float(breaker.get("value"))
    except (TypeError, ValueError):
        raise ValueError("breaker value must be a number")
    if value <= 0:
        raise ValueError("breaker value must be positive")
    if mode == "pct" and value > 100:
        raise ValueError("breaker percent cannot exceed 100")
    return {"enabled": enabled, "mode": mode, "value": round(value, 4)}


def validate_allocation(alloc: dict | None) -> dict:
    """Normalise and bounds-check. Returns the default for None so every row
    has an allocation whether or not the operator set one."""
    if alloc is None:
        return dict(DEFAULT_ALLOCATION)
    mode = str(alloc.get("mode", "pct"))
    if mode not in ALLOCATION_MODES:
        raise ValueError(f"allocation mode must be one of {sorted(ALLOCATION_MODES)}")
    try:
        value = float(alloc.get("value"))
    except (TypeError, ValueError):
        raise ValueError("allocation value must be a number")
    if value <= 0:
        raise ValueError("allocation value must be positive")
    if mode == "pct" and value > 100:
        raise ValueError("allocation percent cannot exceed 100")
    return {"mode": mode, "value": round(value, 4)}


def _uuid() -> str:
    return uuid.uuid4().hex


class StrategyInstanceRow(Base):
    __tablename__ = "strategy_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(48))  # registry key
    # ALSO stamped onto TradePlan.strategy (String(24) fit) as the display
    # label; the linkage itself is trade_plans.strategy_id, not this name.
    name: Mapped[str] = mapped_column(String(24), unique=True)
    params: Mapped[dict] = mapped_column(JSON)
    # {"mode": "pct"|"usd", "value": N}. Nullable so pre-0006 rows read as the
    # default rather than as zero — an unallocated strategy must not silently
    # become one that can trade nothing, or one that can trade everything.
    allocation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {"enabled": bool, "mode": "pct"|"usd", "value": N} — drawdown from this
    # strategy's own high-water mark that flattens its book and pauses it.
    circuit_breaker: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(12), default="disabled")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "params": self.params,
            "allocation": validate_allocation(self.allocation),
            "circuit_breaker": validate_breaker(self.circuit_breaker),
            "state": self.state,
            "created_at": as_utc(self.created_at).isoformat() if self.created_at else None,
            "updated_at": as_utc(self.updated_at).isoformat() if self.updated_at else None,
        }


class StrategyDecisionRow(Base):
    __tablename__ = "strategy_decisions"
    __table_args__ = (Index("ix_decisions_strategy_ts", "strategy_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("strategy_instances.id")
    )
    signal_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    action: Mapped[str] = mapped_column(String(16))
    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": as_utc(self.ts).isoformat() if self.ts else None,
            "strategy_id": self.strategy_id,
            "signal_ids": self.signal_ids,
            "action": self.action,
            "dedupe_key": self.dedupe_key,
            "plan_id": self.plan_id,
            "detail": self.detail,
        }
