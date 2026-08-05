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


def _uuid() -> str:
    return uuid.uuid4().hex


class StrategyInstanceRow(Base):
    __tablename__ = "strategy_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(48))  # registry key
    # ALSO the TradePlan.strategy label (String(24) fit) linking plans back
    # to the instance until the strategy_id FK lands on trade_plans.
    name: Mapped[str] = mapped_column(String(24), unique=True)
    params: Mapped[dict] = mapped_column(JSON)
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
