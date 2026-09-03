"""Journal, feed settings, and exit-lock serialization."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.trade import TradePlan
from app.services.plan_fsm import PlanEvent, PlanStateMachine
from app.services.system_state import DEFAULT_FEED, FeedSettingsService


class _Broadcast:
    def publish(self, topic, msg):
        pass


async def make_plan(db, status="planned") -> str:
    plan = TradePlan(
        underlying="SPY", strategy="long_call",
        legs=[{"symbol": "SPY260731C00450000", "right": "C", "strike": 450.0,
               "expiry": "2026-07-31", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2}],
        qty=1, entry_limit=2.0, tp_premium=4.0, sl_premium=1.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        status=status,
    )
    async with db.session() as session:
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
    return plan.id


class TestJournal:
    @pytest.mark.asyncio
    async def test_applied_and_dropped_events_are_journaled(self, db):
        fsm = PlanStateMachine(db, _Broadcast())
        plan_id = await make_plan(db)
        await fsm.apply(plan_id, PlanEvent.ENTRY_SUBMITTED, entry_order_id="o1")
        await fsm.apply(plan_id, PlanEvent.ENTRY_FILLED, fill_premium=2.1, filled_qty=1)
        # Duplicate fill: dropped, but must still leave an audit row.
        await fsm.apply(plan_id, PlanEvent.ENTRY_FILLED, fill_premium=2.1)
        events = await fsm.events_for(plan_id)
        assert len(events) == 3
        assert events[0]["applied"] is False and "illegal" in events[0]["detail"]
        assert events[1]["event"] == "entry_filled" and events[1]["applied"] is True
        assert events[2]["event"] == "entry_submitted"
        assert "entry_order_id=o1" in events[2]["detail"]

    @pytest.mark.asyncio
    async def test_guard_drop_is_journaled(self, db):
        fsm = PlanStateMachine(db, _Broadcast())
        plan_id = await make_plan(db, status="exiting")
        result = await fsm.apply(
            plan_id, PlanEvent.EXIT_ORDER_DEAD,
            guard={"exit_order_id": "not-the-current-order"},
            exit_order_id=None,
        )
        assert result.applied is False
        events = await fsm.events_for(plan_id)
        assert events[0]["applied"] is False
        assert "guard" in events[0]["detail"]


class TestFeedSettings:
    @pytest.mark.asyncio
    async def test_defaults_roundtrip_and_validation(self, db):
        svc = FeedSettingsService(db)
        cfg = await svc.get()
        for key in DEFAULT_FEED:
            assert key in cfg
        updated = await svc.update({"positions_poll_s": 10, "option_feed": "opra"})
        assert updated["positions_poll_s"] == 10
        assert updated["option_feed"] == "opra"
        # Persisted:
        again = await svc.get()
        assert again["positions_poll_s"] == 10
        with pytest.raises(ValueError):
            await svc.update({"positions_poll_s": 0.1})  # below range
        with pytest.raises(ValueError):
            await svc.update({"stock_feed": "bloomberg"})  # not an option
        with pytest.raises(ValueError):
            await svc.update({"nonsense": 1})
        assert "stock_feed" in (await svc.get())["restart_required_keys"]


class TestExitLock:
    @pytest.mark.asyncio
    async def test_concurrent_exit_triggers_serialize(self, db):
        """Two exit triggers for the same plan must run the ladder strictly
        one-after-the-other, never interleaved."""
        from app.services.exit_enforcer import ExitEnforcer

        enforcer = ExitEnforcer.__new__(ExitEnforcer)  # no full stack needed
        enforcer._exit_locks = {}
        running = {"n": 0, "max": 0}

        async def fake_ladder(plan_id, reason):
            running["n"] += 1
            running["max"] = max(running["max"], running["n"])
            await asyncio.sleep(0.05)
            running["n"] -= 1

        enforcer._execute_exit_locked = fake_ladder
        await asyncio.gather(
            enforcer._execute_exit("p1", "sl"),
            enforcer._execute_exit("p1", "manual"),
            enforcer._execute_exit("p1", "time_stop"),
        )
        assert running["max"] == 1  # never overlapped

    @pytest.mark.asyncio
    async def test_different_plans_do_not_block_each_other(self, db):
        from app.services.exit_enforcer import ExitEnforcer

        enforcer = ExitEnforcer.__new__(ExitEnforcer)
        enforcer._exit_locks = {}
        concurrent = {"n": 0, "max": 0}

        async def fake_ladder(plan_id, reason):
            concurrent["n"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["n"])
            await asyncio.sleep(0.05)
            concurrent["n"] -= 1

        enforcer._execute_exit_locked = fake_ladder
        await asyncio.gather(
            enforcer._execute_exit("p1", "sl"),
            enforcer._execute_exit("p2", "sl"),
        )
        assert concurrent["max"] == 2  # parallel across plans


class TestMarketClockEndpoint:
    """`/api/system/state` reports the enforcer's CACHED clock, which nothing
    populates while no plans are open. This endpoint forces the fetch — and
    must report a calendar it could not read as unknown, never as closed."""

    def _request(self, clock):
        from types import SimpleNamespace

        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            enforcer=SimpleNamespace(clock=clock))))

    @pytest.mark.asyncio
    async def test_forces_the_fetch_and_returns_the_snapshot(self):
        from app.api.routes.system import market_clock

        asked = {"n": 0}

        class Clock:
            async def is_open(self):
                asked["n"] += 1
                return False

            def status(self):
                return {"known": asked["n"] > 0, "is_open": False,
                        "next_open": "2026-08-07T13:30:00+00:00",
                        "next_close": "2026-08-07T20:00:00+00:00"}

        out = await market_clock(self._request(Clock()))
        assert asked["n"] == 1                 # the whole point: it asked
        assert out["known"] is True
        assert out["is_open"] is False
        assert out["next_open"].startswith("2026-08-07")

    @pytest.mark.asyncio
    async def test_unreachable_calendar_is_unknown_not_closed(self):
        from app.api.routes.system import market_clock

        class Clock:
            async def is_open(self):
                raise RuntimeError("broker unreachable")

            def status(self):
                return {"known": False}

        out = await market_clock(self._request(Clock()))
        assert out["known"] is False
        assert out.get("is_open") is None       # not False — we do not know
        assert "broker unreachable" in out["error"]
