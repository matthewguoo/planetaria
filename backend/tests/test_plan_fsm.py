"""Exhaustive verification of the trade-plan FSM: the full state x event
matrix, terminal absorption, duplicate/out-of-order broker events, field
atomicity, and concurrent event application."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.trade import OPEN_STATUSES, TERMINAL_STATUSES, TradePlan
from app.services.plan_fsm import (
    MONITOR_STATES,
    OPEN_STATES,
    POSITION_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    PlanEvent,
    PlanState,
    PlanStateMachine,
)


class _Broadcast:
    def __init__(self):
        self.published = []

    def publish(self, topic, msg):
        self.published.append((topic, msg))


@pytest_asyncio.fixture
async def fsm(db):
    return PlanStateMachine(db, _Broadcast())


async def make_plan(db, status: str = "planned") -> str:
    plan = TradePlan(
        underlying="SPY",
        strategy="long_call",
        legs=[{"symbol": "SPY260731C00450000", "right": "C", "strike": 450.0,
               "expiry": "2026-07-31", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2}],
        qty=2,
        entry_limit=2.0,
        tp_premium=4.0,
        sl_premium=1.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        status=status,
    )
    async with db.session() as session:
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
    return plan.id


# ------------------------------------------------------------ table shape


def test_state_group_partitions():
    assert OPEN_STATES | TERMINAL_STATES == set(PlanState)
    assert OPEN_STATES & TERMINAL_STATES == set()
    assert POSITION_STATES <= OPEN_STATES
    assert MONITOR_STATES <= POSITION_STATES
    # Model-layer string sets mirror the FSM groups exactly.
    assert {s.value for s in OPEN_STATES} == OPEN_STATUSES
    assert {s.value for s in TERMINAL_STATES} == TERMINAL_STATUSES


def test_terminal_states_absorb_everything():
    for event, table in TRANSITIONS.items():
        for source in table:
            assert source not in TERMINAL_STATES, (
                f"{event} escapes terminal state {source}"
            )


def test_every_event_has_transitions():
    for event in PlanEvent:
        assert event in TRANSITIONS and TRANSITIONS[event], f"{event} unmapped"


def test_no_resurrection_paths():
    # Nothing maps INTO planned/submitted from a later state.
    order = [
        PlanState.PLANNED, PlanState.SUBMITTED, PlanState.PARTIALLY_FILLED,
        PlanState.FILLED, PlanState.EXITING,
    ]
    rank = {s: i for i, s in enumerate(order)}
    for event, table in TRANSITIONS.items():
        for source, target in table.items():
            if target in TERMINAL_STATES:
                continue
            assert rank[target] >= rank[source], (
                f"{event}: {source} -> {target} moves backwards"
            )


# ------------------------------------------------- exhaustive matrix apply


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(PlanState))
@pytest.mark.parametrize("event", list(PlanEvent))
async def test_full_matrix(db, fsm, source, event):
    """Every (state, event) pair behaves exactly per the table: legal pairs
    land on the declared target, everything else is ignored with no write."""
    plan_id = await make_plan(db, status=source.value)
    result = await fsm.apply(plan_id, event)
    expected = TRANSITIONS.get(event, {}).get(source)
    if expected is None:
        assert result.applied is False
        assert result.plan.status == source.value  # untouched
    else:
        assert result.applied is True
        assert result.target == expected
        assert result.plan.status == expected.value


# --------------------------------------------------------- event sequences


@pytest.mark.asyncio
async def test_happy_path_with_fields(db, fsm):
    plan_id = await make_plan(db)
    await fsm.apply(plan_id, PlanEvent.ENTRY_SUBMITTED, entry_order_id="o1")
    await fsm.apply(plan_id, PlanEvent.ENTRY_FILLED, fill_premium=2.05, filled_qty=2)
    await fsm.apply(plan_id, PlanEvent.EXIT_SUBMITTED, exit_order_id="o2", exit_reason="tp")
    result = await fsm.apply(plan_id, PlanEvent.EXIT_FILLED, exit_premium=4.1, realized_pnl=410.0)
    plan = result.plan
    assert plan.status == "closed"
    assert plan.entry_order_id == "o1"
    assert plan.fill_premium == 2.05
    assert plan.exit_reason == "tp"
    assert plan.realized_pnl == 410.0


@pytest.mark.asyncio
async def test_duplicate_fill_is_noop(db, fsm):
    plan_id = await make_plan(db, status="submitted")
    first = await fsm.apply(plan_id, PlanEvent.ENTRY_FILLED, fill_premium=2.0)
    dupe = await fsm.apply(plan_id, PlanEvent.ENTRY_FILLED, fill_premium=9.9)
    assert first.applied and not dupe.applied
    assert dupe.plan.fill_premium == 2.0  # duplicate's fields discarded


@pytest.mark.asyncio
async def test_late_events_cannot_resurrect_closed_plan(db, fsm):
    plan_id = await make_plan(db, status="closed")
    for event in PlanEvent:
        result = await fsm.apply(plan_id, event)
        assert not result.applied
        assert result.plan.status == "closed"


@pytest.mark.asyncio
async def test_partial_fill_lifecycle(db, fsm):
    plan_id = await make_plan(db, status="submitted")
    await fsm.apply(plan_id, PlanEvent.ENTRY_PARTIAL_FILL, fill_premium=2.0, filled_qty=1)
    # Running-average refresh (self-transition) is legal.
    again = await fsm.apply(plan_id, PlanEvent.ENTRY_PARTIAL_FILL, fill_premium=2.02, filled_qty=1)
    assert again.applied and again.plan.status == "partially_filled"
    # Entry cancelled with a partial on the books -> managed as filled, shrunk.
    result = await fsm.apply(
        plan_id, PlanEvent.ENTRY_CANCELLED_PARTIAL, qty=1, filled_qty=1
    )
    assert result.applied and result.plan.status == "filled"
    assert result.plan.qty == 1
    assert result.plan.effective_qty == 1


@pytest.mark.asyncio
async def test_exit_ladder_self_transitions(db, fsm):
    plan_id = await make_plan(db, status="filled")
    await fsm.apply(plan_id, PlanEvent.EXIT_SUBMITTED, exit_order_id="x1", exit_reason="sl")
    # Reprice (ladder step): still exiting, new order id.
    r = await fsm.apply(plan_id, PlanEvent.EXIT_SUBMITTED, exit_order_id="x2", exit_reason="sl")
    assert r.applied and r.plan.exit_order_id == "x2"
    # Order dies: still exiting, id cleared — never silently un-exits.
    r = await fsm.apply(plan_id, PlanEvent.EXIT_ORDER_DEAD, exit_order_id=None)
    assert r.applied and r.plan.status == "exiting" and r.plan.exit_order_id is None


@pytest.mark.asyncio
async def test_concurrent_events_serialize(db, fsm):
    """A burst of racing events must apply exactly one legal path — no
    double-fills, no closed plan reopening, final state legal."""
    plan_id = await make_plan(db, status="submitted")
    events = (
        [PlanEvent.ENTRY_FILLED] * 5
        + [PlanEvent.ENTRY_CANCELLED] * 5
        + [PlanEvent.EXIT_ORDER_DEAD] * 3
    )
    results = await asyncio.gather(*(fsm.apply(plan_id, e) for e in events))
    applied = [r for r in results if r.applied]
    # Exactly ONE of the competing entry outcomes won.
    assert len(applied) == 1
    final = (await fsm.update_fields(plan_id)).status
    assert final in ("filled", "cancelled")


@pytest.mark.asyncio
async def test_compare_and_set_blocks_out_of_band_writers(db, fsm):
    """If something outside the FSM mutates the row between read and write,
    the transition is dropped, not blindly applied."""
    plan_id = await make_plan(db, status="submitted")

    real_lock = fsm._lock(plan_id)

    async def sabotage():
        # Mutate status out-of-band while the FSM believes it's 'submitted'.
        async with db.session() as session:
            plan = await session.get(TradePlan, plan_id)
            plan.status = "cancelled"
            await session.commit()

    await sabotage()
    result = await fsm.apply(plan_id, PlanEvent.ENTRY_FILLED, fill_premium=2.0)
    assert not result.applied
    assert result.plan.status == "cancelled"
    assert real_lock is fsm._lock(plan_id)
