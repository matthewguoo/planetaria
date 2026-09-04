"""The 2026-09-03 fly-1 incident, replayed as tests.

What happened: Alpaca's desk bought back the short 718 call at 15:45 (five
minutes before the strategy's time stop), every 4-leg close after that was
unfillable, the 16:00 re-triggers PARKED the exit "until the next open" on a
structure that ceased to exist at the bell, and the short 718 put expired
33c ITM into an assignment. Four distinct behaviors are pinned here:

1. a close matches what the broker HOLDS, not what the plan says;
2. an exit on a fully-expired structure settles instead of parking;
3. reconcile captures a vanished position even when a time stop is overdue
   (the backstop must not shadow the capture forever);
4. capture values expired never-closed legs at intrinsic, so the record
   carries the real number, not a blank.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.exit_enforcer import ExitEnforcer
from app.services.risk import RiskService
from app.services.trade_service import TradeService

SC = "QQQ260903C00718000"   # short call — the leg the desk clipped
SP = "QQQ260903P00718000"   # short put — expired 33c ITM
LC = "QQQ260903C00725000"   # long wings — expired worthless
LP = "QQQ260903P00711000"
T0 = datetime(2026, 9, 3, 18, 0, 48, tzinfo=timezone.utc)   # 14:00:48 ET
NEXT_OPEN = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)

FLY_LEGS = [
    {"symbol": SC, "right": "C", "strike": 718.0, "expiry": "2026-09-03",
     "side": -1, "ratio": 1, "entry": 0.845, "iv": 0.0},
    {"symbol": SP, "right": "P", "strike": 718.0, "expiry": "2026-09-03",
     "side": -1, "ratio": 1, "entry": 0.655, "iv": 0.0},
    {"symbol": LC, "right": "C", "strike": 725.0, "expiry": "2026-09-03",
     "side": 1, "ratio": 1, "entry": 0.025, "iv": 0.0},
    {"symbol": LP, "right": "P", "strike": 711.0, "expiry": "2026-09-03",
     "side": 1, "ratio": 1, "entry": 0.03, "iv": 0.0},
]


class _Broadcast:
    def publish(self, topic, msg):
        pass


class _Market:
    def __init__(self):
        self.broadcast = _Broadcast()
        self.quotes = {}

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)


class _Clock:
    """MarketClock stand-in with a pinned session state."""

    def __init__(self, is_open: bool, next_open_at=None):
        self._open = is_open
        self._next = next_open_at

    async def is_open(self):
        return self._open

    async def next_open(self):
        return self._next


def _order(**kw):
    defaults = dict(
        id="", client_order_id="", symbol=None, legs=None, filled_qty=0,
        filled_avg_price=0, position_intent=None, filled_at=None, submitted_at=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _desk_history(plan_id: str) -> list:
    """Broker order history: our entry MLEG plus the desk's 15:45 single-leg
    buyback of the short call at 0.13 — exactly what the paper account
    showed on 2026-09-03."""
    entry = _order(
        id="entry-1", client_order_id=f"{plan_id}-e",
        filled_qty=1, filled_avg_price=-1.44, submitted_at=T0, filled_at=T0,
        legs=[
            _order(id=f"entry-1{i}", client_order_id=f"broker-gen-{i}",
                   symbol=leg["symbol"], filled_qty=1,
                   filled_avg_price=abs(leg["entry"]),
                   position_intent=("sell_to_open" if leg["side"] < 0 else "buy_to_open"),
                   filled_at=T0)
            for i, leg in enumerate(FLY_LEGS)
        ],
    )
    desk = _order(
        id="desk-1", client_order_id="broker-desk-1", symbol=SC,
        filled_qty=1, filled_avg_price=0.13, position_intent="buy_to_close",
        submitted_at=T0 + timedelta(hours=1, minutes=44),
        filled_at=T0 + timedelta(hours=1, minutes=45),
    )
    return [desk, entry]


@pytest_asyncio.fixture
async def env(tmp_path):
    ns = SimpleNamespace()
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")

    ns.positions = []          # what get_all_positions returns
    ns.submitted = []          # orders submit_order received

    async def fake_call(fn, *args, **kwargs):
        name = getattr(fn, "__name__", "")
        if name == "get_orders":
            return _desk_history(ns.plan_id)
        if name == "get_all_positions":
            return ns.positions
        raise AssertionError(f"unexpected broker call {fn}")

    def get_orders(*a, **k):  # named: fake_call dispatches on __name__
        raise AssertionError("must go through alpaca.call")

    def get_all_positions(*a, **k):
        raise AssertionError("must go through alpaca.call")

    alpaca = SimpleNamespace(
        configured=True,
        call=fake_call,
        trading=SimpleNamespace(
            get_orders=get_orders,
            get_all_positions=get_all_positions,
        ),
    )
    market = _Market()
    # Underlying mark = the actual 2026-09-03 close.
    market.quotes["QQQ"] = {"bid": 717.66, "ask": 717.68, "mid": 717.67}
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade,
                            clock=_Clock(is_open=False, next_open_at=NEXT_OPEN))
    # Pin the wall clock to the incident evening: the legs expire 2026-09-03
    # and "today" must be that date, whatever day the suite runs on.
    enforcer._now = lambda: T0 + timedelta(hours=2, minutes=30)   # 16:30 ET

    async with db.session() as session:
        plan = TradePlan(
            underlying="QQQ", strategy="fly-1",
            legs=[dict(leg) for leg in FLY_LEGS],
            qty=1, entry_limit=-1.43,
            tp_premium=None, sl_premium=None,
            time_stop_utc=T0 + timedelta(hours=1, minutes=50),
            status="filled", filled_qty=1,
            entry_order_id="entry-1", fill_premium=-1.44,
            created_at=T0,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        session.add(plan)
        await session.commit()
        ns.plan_id = plan.id

    ns.db, ns.trade, ns.enforcer, ns.market = db, trade, enforcer, market
    yield ns
    await db.close()


def _pos(symbol: str, qty: float) -> SimpleNamespace:
    """Shaped like the SDK position object broker_positions normalizes."""
    return SimpleNamespace(symbol=symbol, qty=qty, asset_class="us_option")


class TestCaptureValuesExpiredLegsAtIntrinsic:
    @pytest.mark.asyncio
    async def test_desk_fill_plus_intrinsic_settles_the_structure(self, env):
        plan = await env.trade.get_plan(env.plan_id)
        premium, sets_closed, _exited_at, _events, detail = (
            await env.enforcer._capture_external_exit(plan)
        )
        # -0.13 (desk buyback) - 0.33 (718 put intrinsic vs 717.67) + 0 + 0
        assert premium == pytest.approx(-0.46, abs=1e-4)
        assert sets_closed == 1
        assert "intrinsic" in detail

    @pytest.mark.asyncio
    async def test_a_live_leg_is_never_valued_at_intrinsic(self, env):
        """Only legs that can never trade again get the intrinsic shortcut;
        a missing fill on a live leg still falls to the mark fallback."""
        env.enforcer.clock = _Clock(is_open=True)   # expiry day, market open
        plan = await env.trade.get_plan(env.plan_id)
        premium, sets_closed, _e, _ev, detail = (
            await env.enforcer._capture_external_exit(plan)
        )
        assert sets_closed is None          # no full per-leg settlement
        assert "intrinsic" not in detail


class TestExpiredExitSettlesInsteadOfParking:
    @pytest.mark.asyncio
    async def test_execute_exit_after_the_bell_force_closes(self, env):
        await env.enforcer._execute_exit(env.plan_id, "time_stop")
        closed = await env.trade.get_plan(env.plan_id)
        assert closed.status == "closed"
        assert closed.exit_premium == pytest.approx(-0.46, abs=1e-4)
        # (-0.46 - (-1.44)) * 100 * 1 = the +$98 the fly actually made
        assert closed.realized_pnl == pytest.approx(98.0, abs=0.5)
        assert env.plan_id not in env.enforcer._parked

    @pytest.mark.asyncio
    async def test_not_yet_expired_plan_still_parks(self, env):
        """The morning of expiry day (market not open yet) the legs still
        have a session ahead — parking remains correct there."""
        env.enforcer.clock = _Clock(
            is_open=False,
            next_open_at=datetime(2026, 9, 3, 13, 30, tzinfo=timezone.utc),
        )
        env.positions = [_pos(s, 1) for s in (SC, SP, LC, LP)]
        for leg in FLY_LEGS:
            env.market.quotes[leg["symbol"]] = {
                "bid": abs(leg["entry"]) - 0.01, "ask": abs(leg["entry"]) + 0.01,
                "mid": abs(leg["entry"]),
            }

        async def record_submit(plan, reason, limit, attempt_key="0", **kw):
            env.submitted.append((limit, kw))

        env.trade.submit_exit = record_submit
        await env.enforcer._execute_exit(env.plan_id, "time_stop")
        assert env.plan_id in env.enforcer._parked
        assert len(env.submitted) == 1
        still_open = await env.trade.get_plan(env.plan_id)
        assert still_open.status == "filled"


class TestReconcileCapturesBeforeTheBackstop:
    @pytest.mark.asyncio
    async def test_vanished_position_with_overdue_stop_is_captured_not_laddered(self, env):
        """The backstop returns early on every pass while a stop is overdue;
        the vanished-position check must come first or the plan ladders
        forever against a position that no longer exists."""
        env.positions = []                  # broker holds nothing

        async def never_ladder(plan_id, reason):
            raise AssertionError("backstop laddered a vanished position")

        env.enforcer._execute_exit = never_ladder
        plan = await env.trade.get_plan(env.plan_id)
        await env.enforcer._reconcile_plan(plan)
        closed = await env.trade.get_plan(env.plan_id)
        assert closed.status == "closed"
        assert closed.realized_pnl == pytest.approx(98.0, abs=0.5)


class TestCloseMatchesBrokerHolds:
    @pytest.mark.asyncio
    async def test_held_close_legs_drops_the_clipped_leg(self, env):
        env.positions = [_pos(SP, -1), _pos(LC, 1), _pos(LP, 1)]   # call clipped
        plan = await env.trade.get_plan(env.plan_id)
        legs, sets = await env.enforcer._held_close_legs(plan)
        assert [leg["symbol"] for leg in legs] == [SP, LC, LP]
        assert sets == 1

    @pytest.mark.asyncio
    async def test_full_holds_pass_through_untouched(self, env):
        env.positions = [_pos(SC, -1), _pos(SP, -1), _pos(LC, 1), _pos(LP, 1)]
        plan = await env.trade.get_plan(env.plan_id)
        legs, sets = await env.enforcer._held_close_legs(plan)
        assert legs is plan.legs and sets == 1

    @pytest.mark.asyncio
    async def test_broker_error_degrades_to_the_full_structure(self, env):
        async def broken_positions(max_age_s=5.0):
            raise RuntimeError("positions API down")

        env.trade.broker_positions = broken_positions
        plan = await env.trade.get_plan(env.plan_id)
        legs, sets = await env.enforcer._held_close_legs(plan)
        assert legs is plan.legs and sets == 1

    @pytest.mark.asyncio
    async def test_ladder_submits_the_reduced_structure(self, env):
        env.enforcer.clock = _Clock(is_open=True)
        env.positions = [_pos(SP, -1), _pos(LC, 1), _pos(LP, 1)]
        for leg in FLY_LEGS:
            env.market.quotes[leg["symbol"]] = {
                "bid": abs(leg["entry"]) - 0.01, "ask": abs(leg["entry"]) + 0.01,
                "mid": abs(leg["entry"]),
            }
        env.enforcer.escalation = [(None, 0.0)]     # one market rung

        async def record_submit(plan, reason, limit, attempt_key="0", **kw):
            env.submitted.append(kw)

        async def no_verify(plan_id, reason):
            pass

        env.trade.submit_exit = record_submit
        env.enforcer._verify_closed = no_verify
        await env.enforcer._execute_exit(env.plan_id, "time_stop")
        assert len(env.submitted) == 1
        kw = env.submitted[0]
        assert [leg["symbol"] for leg in kw["close_legs"]] == [SP, LC, LP]
        assert kw["close_sets"] == 1

    @pytest.mark.asyncio
    async def test_nothing_held_mid_ladder_captures_instead(self, env):
        env.enforcer.clock = _Clock(is_open=True)   # legs NOT expired yet
        env.positions = []
        for leg in FLY_LEGS:
            env.market.quotes[leg["symbol"]] = {
                "bid": abs(leg["entry"]) - 0.01, "ask": abs(leg["entry"]) + 0.01,
                "mid": abs(leg["entry"]),
            }

        submitted = []

        async def record_submit(*a, **k):
            submitted.append(k)

        async def no_verify(plan_id, reason):
            pass

        # Recorded rather than raising: the ladder swallows submit errors by
        # design, so a raising stub would spin the retry loop, not fail.
        env.trade.submit_exit = record_submit
        env.enforcer._verify_closed = no_verify
        await env.enforcer._execute_exit(env.plan_id, "time_stop")
        assert submitted == [], "submitted a close for a vanished position"
        closed = await env.trade.get_plan(env.plan_id)
        assert closed.status == "closed"
        # Market still open, so no intrinsic shortcut: the desk fill covers
        # one leg only, capture falls to the last defensible mark.
        # mid = -0.845 - 0.655 + 0.025 + 0.03 -> pnl (-1.445 + 1.44) * 100
        assert closed.realized_pnl == pytest.approx(-0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_close_request_builds_the_override_shape(self, env):
        plan = await env.trade.get_plan(env.plan_id)
        held = [leg for leg in plan.legs if leg["symbol"] != SC]
        request = env.trade._close_request(plan, None, "coid-1", legs=held, sets=1)
        assert request.qty == 1
        assert [leg.symbol for leg in request.legs] == [SP, LC, LP]
        # One remaining leg degrades to a SIMPLE close, not a 1-leg MLEG.
        single = env.trade._close_request(plan, None, "coid-2", legs=[held[0]], sets=1)
        assert single.symbol == SP and single.qty == 1
