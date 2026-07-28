r"""Trade-plan lifecycle as an explicit finite state machine.

Modeled on the FIX protocol order state model (FIX spec Appendix D "Order
State Change Matrices"): a declarative transition table where every
(event, current-state) pair either names the single legal target state or is
absent — absent means the event is IGNORED in that state (logged, no write).
Terminal states absorb everything; a closed plan can never be resurrected by
a late or duplicate broker event.

Why hand-rolled rather than pytransitions / python-statemachine: those bind
state to in-memory objects. Our authoritative state is a DB row mutated by
several concurrent async tasks (trading-stream callbacks, exit-enforcer
monitors, REST handlers, startup reconcile). Robustness here means
per-plan serialization plus an atomic compare-and-set on the row
(UPDATE ... WHERE status = expected), which generic FSM libraries don't
provide. The table itself is ~40 lines and exhaustively unit-tested over the
full state x event matrix — nothing a dependency would improve.

State diagram:

    planned --ENTRY_SUBMITTED--> submitted --ENTRY_FILLED----------> filled
       |                            |  \--ENTRY_PARTIAL_FILL--> partially_filled
       |                            |         |     \--ENTRY_FILLED--> filled
       |                            |         \--ENTRY_CANCELLED_PARTIAL--> filled (qty shrinks)
       |                            \--ENTRY_CANCELLED/REJECTED--> cancelled/rejected
       \--ENTRY_CANCELLED--> cancelled
    filled|partially_filled --EXIT_SUBMITTED--> exiting --EXIT_FILLED--> closed
    exiting --EXIT_ORDER_DEAD--> exiting (order id cleared; ladder resubmits)
    filled|partially_filled|exiting --FORCE_CLOSED--> closed (external liquidation)
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import update

from app.models.trade import TradePlan

log = logging.getLogger("app.fsm")


class PlanState(str, Enum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    EXITING = "exiting"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class PlanEvent(str, Enum):
    ENTRY_SUBMITTED = "entry_submitted"
    ENTRY_REJECTED = "entry_rejected"
    ENTRY_PARTIAL_FILL = "entry_partial_fill"
    ENTRY_FILLED = "entry_filled"
    ENTRY_CANCELLED = "entry_cancelled"
    # Entry died with a partial position on the books: plan shrinks to the
    # filled quantity and is managed as filled from here on.
    ENTRY_CANCELLED_PARTIAL = "entry_cancelled_partial"
    EXIT_SUBMITTED = "exit_submitted"
    EXIT_ORDER_DEAD = "exit_order_dead"
    EXIT_FILLED = "exit_filled"
    # Position vanished at the broker outside our exit path (expiry
    # liquidation, manual close in their UI): reconcile closes the plan.
    FORCE_CLOSED = "force_closed"


S = PlanState
E = PlanEvent

# event -> {legal source -> target}. Absence = event ignored in that state.
TRANSITIONS: dict[PlanEvent, dict[PlanState, PlanState]] = {
    E.ENTRY_SUBMITTED: {S.PLANNED: S.SUBMITTED},
    E.ENTRY_REJECTED: {S.PLANNED: S.REJECTED, S.SUBMITTED: S.REJECTED},
    E.ENTRY_PARTIAL_FILL: {
        S.SUBMITTED: S.PARTIALLY_FILLED,
        S.PARTIALLY_FILLED: S.PARTIALLY_FILLED,  # running-average refresh
    },
    E.ENTRY_FILLED: {S.SUBMITTED: S.FILLED, S.PARTIALLY_FILLED: S.FILLED},
    E.ENTRY_CANCELLED: {S.PLANNED: S.CANCELLED, S.SUBMITTED: S.CANCELLED},
    E.ENTRY_CANCELLED_PARTIAL: {S.PARTIALLY_FILLED: S.FILLED},
    E.EXIT_SUBMITTED: {
        S.FILLED: S.EXITING,
        S.PARTIALLY_FILLED: S.EXITING,  # entry cancel may still be in flight
        S.EXITING: S.EXITING,  # escalation ladder repricing
    },
    E.EXIT_ORDER_DEAD: {S.EXITING: S.EXITING},
    E.EXIT_FILLED: {S.EXITING: S.CLOSED},
    E.FORCE_CLOSED: {
        S.PARTIALLY_FILLED: S.CLOSED,
        S.FILLED: S.CLOSED,
        S.EXITING: S.CLOSED,
    },
}

TERMINAL_STATES = {S.CLOSED, S.CANCELLED, S.REJECTED}
OPEN_STATES = {S.PLANNED, S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.EXITING}
# States in which the account may hold contracts for this plan.
POSITION_STATES = {S.PARTIALLY_FILLED, S.FILLED, S.EXITING}
# States the exit enforcer watches quotes for (TP/SL can trigger).
MONITOR_STATES = {S.PARTIALLY_FILLED, S.FILLED}


@dataclass
class TransitionResult:
    applied: bool
    plan: TradePlan
    event: PlanEvent
    source: PlanState
    target: PlanState | None  # None when the event was ignored


class PlanStateMachine:
    """Applies events to plans atomically.

    Per-plan asyncio.Lock serializes concurrent events inside this process;
    the compare-and-set UPDATE (WHERE status = expected) is the safety net
    against anything else touching the row. An event whose (state, event)
    pair is not in the table is a no-op with a log line — never an exception
    and never a write — so duplicate and out-of-order broker events are
    harmless by construction.
    """

    def __init__(self, db, broadcaster):
        self.db = db
        self.broadcast = broadcaster
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, plan_id: str) -> asyncio.Lock:
        lock = self._locks.get(plan_id)
        if lock is None:
            lock = self._locks[plan_id] = asyncio.Lock()
        return lock

    async def apply(self, plan_id: str, event: PlanEvent, **fields) -> TransitionResult:
        async with self._lock(plan_id):
            async with self.db.session() as session:
                plan = await session.get(TradePlan, plan_id)
                if plan is None:
                    raise ValueError(f"no plan {plan_id}")
                source = PlanState(plan.status)
                target = TRANSITIONS.get(event, {}).get(source)
                if target is None:
                    log.warning(
                        "plan %s: event %s ignored in state %s",
                        plan_id, event.value, source.value,
                    )
                    return TransitionResult(False, plan, event, source, None)

                result = await session.execute(
                    update(TradePlan)
                    .where(TradePlan.id == plan_id, TradePlan.status == source.value)
                    .values(status=target.value, **fields)
                )
                await session.commit()
                if result.rowcount == 0:
                    # Out-of-band writer beat us (multi-process safety net).
                    log.error(
                        "plan %s: compare-and-set lost for %s (%s -> %s) - event dropped",
                        plan_id, event.value, source.value, target.value,
                    )
                    plan = await session.get(TradePlan, plan_id, populate_existing=True)
                    return TransitionResult(False, plan, event, source, None)
                plan = await session.get(TradePlan, plan_id, populate_existing=True)

            log.info(
                "plan %s: %s --%s--> %s", plan_id, source.value, event.value, target.value
            )
            self.broadcast.publish("plans", {"t": "plan", "plan": plan.to_dict()})
            # Locks are deliberately never removed: popping one while a waiter
            # holds a reference would let two locks guard the same plan. A few
            # bytes per plan per process lifetime is the cheaper invariant.
            return TransitionResult(True, plan, event, source, target)

    async def update_fields(self, plan_id: str, **fields) -> TradePlan:
        """Field-only update (exit tightening, notes) — state unchanged, same
        serialization and broadcast path as real transitions."""
        async with self._lock(plan_id):
            async with self.db.session() as session:
                plan = await session.get(TradePlan, plan_id)
                if plan is None:
                    raise ValueError(f"no plan {plan_id}")
                for key, value in fields.items():
                    setattr(plan, key, value)
                await session.commit()
                await session.refresh(plan)
            self.broadcast.publish("plans", {"t": "plan", "plan": plan.to_dict()})
            return plan
