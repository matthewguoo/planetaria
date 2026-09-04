"""StrategyRunner: registry lifecycle, event dispatch, the intent gate
(equity phase-gate, stale-event guard, dedupe, budgets), crash isolation,
and DB-is-source-of-truth restarts."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.session import Database
from app.models.strategies import StrategyDecisionRow
from app.models.trade import TradePlan
from app.services.risk import RiskService
from app.services.signals.events import Event, EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.strategies import REGISTRY, register
from app.strategies.base import Strategy, TradeIntent


def option_intent(**overrides) -> TradeIntent:
    base = dict(
        asset_class="option", underlying="SPY",
        legs=[{"symbol": "SPY261218C00800000", "right": "C", "strike": 800.0,
               "expiry": "2026-12-18", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2}],
        qty=1, entry_limit=2.0, tp=4.0, sl=1.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        reason="test",
    )
    base.update(overrides)
    return TradeIntent(**base)


class DummyStrategy(Strategy):
    """Test double: submits an option intent when told to."""

    kind = "dummy"
    subscriptions = ("news", "manual")
    default_params = {"knob": 1}

    def __init__(self):
        self.events: list[Event] = []
        self.results: list = []

    async def on_event(self, event, ctx):
        self.events.append(event)
        if event.payload.get("boom"):
            raise RuntimeError("strategy blew up")
        if event.payload.get("hang"):
            await asyncio.sleep(3600)
        if event.payload.get("submit"):
            try:
                self.results.append(await ctx.submit(option_intent(
                    dedupe_key=event.payload.get("dedupe_key"),
                    signal_ids=tuple(event.payload.get("signal_ids", ())),
                )))
            except ValueError as exc:
                self.results.append(exc)


class FakeTrade:
    def __init__(self):
        self.placed: list[dict] = []
        self.enforcer = SimpleNamespace(manual_close=self._close)
        self.closed: list[str] = []

    async def place_trade(self, payload: dict) -> dict:
        self.placed.append(payload)
        return {"id": f"plan{len(self.placed)}", "status": "submitted"}

    async def get_account(self) -> dict:
        return {"equity": 100_000.0, "status": "ACTIVE"}

    async def _close(self, plan_id: str):
        self.closed.append(plan_id)


@pytest.fixture
def dummy_registered():
    if "dummy" not in REGISTRY:
        register(DummyStrategy)
    yield
    REGISTRY.pop("dummy", None)


@pytest_asyncio.fixture
async def rig(tmp_path, dummy_registered):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/runner.db")
    bus = EventBus()
    store = SignalStore(db)
    trade = FakeTrade()
    runner = StrategyRunner(
        db, bus, store, trade, RiskService(db), market=None, clock=None,
        settings=SimpleNamespace(strategies_enabled=True),
    )
    yield SimpleNamespace(db=db, bus=bus, store=store, trade=trade, runner=runner)
    await runner.shutdown()
    await db.close()


async def wait_for(predicate, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def news_event(**payload) -> Event:
    return Event(type="news", ts=datetime.now(timezone.utc), source="test",
                 key=None, symbols=("SPY",), payload=payload)


async def actions_for(db, row_id) -> list[str]:
    async with db.session() as session:
        rows = (await session.scalars(
            select(StrategyDecisionRow)
            .where(StrategyDecisionRow.strategy_id == row_id)
            .order_by(StrategyDecisionRow.id)
        )).all()
    return [r.action for r in rows]


async def actions_once(db, row_id, predicate, timeout=3.0) -> list[str]:
    """The journal for `row_id` once `predicate(actions)` holds (or the
    deadline passes). A bus-driven decision journals AFTER the side effect
    the test waited on (the placement, the strategy's event count), so
    reading the journal the instant the effect appears is a race."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        actions = await actions_for(db, row_id)
        if predicate(actions) or asyncio.get_event_loop().time() >= deadline:
            return actions
        await asyncio.sleep(0.02)


class TestRegistry:
    @pytest.mark.asyncio
    async def test_create_validates_kind_name_params(self, rig):
        with pytest.raises(ValueError):
            await rig.runner.create("nope", "x", {})
        with pytest.raises(ValueError):
            await rig.runner.create("dummy", "x" * 25, {})
        with pytest.raises(ValueError):
            await rig.runner.create("dummy", "ok", {"bad_param": 1})
        row = await rig.runner.create("dummy", "ok", {"knob": 2, "budget": {}})
        assert row["state"] == "disabled"
        assert row["params"]["knob"] == 2
        with pytest.raises(ValueError):  # duplicate name
            await rig.runner.create("dummy", "ok", {})

    @pytest.mark.asyncio
    async def test_enable_dispatch_pause(self, rig):
        row = await rig.runner.create("dummy", "d1", {})
        await rig.runner.set_state(row["id"], "enabled")
        strategy = rig.runner._running[row["id"]].strategy

        rig.bus.publish(news_event(hello=1))
        assert await wait_for(lambda: len(strategy.events) == 1)

        await rig.runner.set_state(row["id"], "paused")
        rig.bus.publish(news_event(hello=2))
        await asyncio.sleep(0.1)
        assert len(strategy.events) == 1  # unsubscribed, nothing delivered

    @pytest.mark.asyncio
    async def test_manual_events_scoped_to_target(self, rig):
        row_a = await rig.runner.create("dummy", "a", {})
        row_b = await rig.runner.create("dummy", "b", {})
        await rig.runner.set_state(row_a["id"], "enabled")
        await rig.runner.set_state(row_b["id"], "enabled")
        strat_a = rig.runner._running[row_a["id"]].strategy
        strat_b = rig.runner._running[row_b["id"]].strategy

        rig.bus.publish(Event(type="manual", ts=datetime.now(timezone.utc),
                              source="test", key=None, symbols=(),
                              payload={"strategy_id": row_a["id"]}))
        assert await wait_for(lambda: len(strat_a.events) == 1)
        await asyncio.sleep(0.1)
        assert len(strat_b.events) == 0

    @pytest.mark.asyncio
    async def test_restart_reconstructs_from_rows(self, rig):
        row = await rig.runner.create("dummy", "persist", {})
        await rig.runner.set_state(row["id"], "enabled")
        await rig.runner.shutdown()

        runner2 = StrategyRunner(
            rig.db, rig.bus, rig.store, rig.trade, RiskService(rig.db),
            market=None, clock=None,
            settings=SimpleNamespace(strategies_enabled=True),
        )
        await runner2.start()
        assert row["id"] in runner2._running
        await runner2.shutdown()

    @pytest.mark.asyncio
    async def test_kill_switch_config(self, rig):
        row = await rig.runner.create("dummy", "killed", {})
        await rig.runner.set_state(row["id"], "enabled")
        await rig.runner.shutdown()

        runner2 = StrategyRunner(
            rig.db, rig.bus, rig.store, rig.trade, RiskService(rig.db),
            market=None, clock=None,
            settings=SimpleNamespace(strategies_enabled=False),
        )
        await runner2.start()
        assert runner2._running == {}


class TestIntentGate:
    @pytest.mark.asyncio
    async def test_option_intent_places_and_journals(self, rig):
        row = await rig.runner.create("dummy", "placer", {"live": True})
        await rig.runner.set_state(row["id"], "enabled")
        rig.bus.publish(news_event(submit=True))
        assert await wait_for(lambda: len(rig.trade.placed) == 1)
        payload = rig.trade.placed[0]
        assert payload["strategy"] == "placer"
        assert payload["underlying"] == "SPY"
        actions = await actions_once(rig.db, row["id"], lambda a: "placed" in a)
        assert actions == ["intent", "placed"]

    @pytest.mark.asyncio
    async def test_equity_intent_flows_with_flags(self, rig):
        row = await rig.runner.create("dummy", "eq", {"live": True})
        await rig.runner.set_state(row["id"], "enabled")
        await rig.runner.execute_intent(row["id"], option_intent(
            asset_class="equity", extended_hours=True,
            legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 700.0}],
            entry_limit=700.0, tp=703.0, sl=696.0,
        ))
        payload = rig.trade.placed[-1]
        assert payload["asset_class"] == "equity"
        assert payload["extended_hours"] is True
        assert payload["strategy_id"] == row["id"]

    @pytest.mark.asyncio
    async def test_dedupe_key_places_once_ever(self, rig):
        row = await rig.runner.create("dummy", "dedupe", {"live": True})
        await rig.runner.set_state(row["id"], "enabled")
        await rig.runner.execute_intent(row["id"], option_intent(dedupe_key="k"))
        with pytest.raises(ValueError, match="dedupe"):
            await rig.runner.execute_intent(row["id"], option_intent(dedupe_key="k"))
        assert len(rig.trade.placed) == 1
        actions = await actions_for(rig.db, row["id"])
        assert actions.count("placed") == 1 and actions[-1] == "skip"

    @pytest.mark.asyncio
    async def test_stale_event_guard(self, rig):
        row = await rig.runner.create("dummy", "stale", {})
        await rig.runner.set_state(row["id"], "enabled")
        old = Event(type="news", ts=datetime.now(timezone.utc) - timedelta(hours=1),
                    source="test", key="old-news", symbols=("SPY",),
                    payload={"headline": "ancient"})
        old, _ = await rig.store.record(old)
        with pytest.raises(ValueError, match="stale"):
            await rig.runner.execute_intent(row["id"], option_intent(
                signal_ids=(old.signal_id,)))
        assert rig.trade.placed == []
        assert (await actions_for(rig.db, row["id"]))[-1] == "skip"

    @pytest.mark.asyncio
    async def test_budget_caps_layered(self, rig):
        row = await rig.runner.create("dummy", "capped", {"budget": {
            "max_open_plans": 1, "allowed_symbols": ["SPY"],
            "max_trade_notional_usd": 1000,
        }})
        await rig.runner.set_state(row["id"], "enabled")

        with pytest.raises(ValueError, match="allowlist"):
            await rig.runner.execute_intent(row["id"], option_intent(underlying="AMD"))
        with pytest.raises(ValueError, match="notional"):
            await rig.runner.execute_intent(row["id"], option_intent(
                entry_limit=20.0))  # 20*100*1 = $2000 > $1000

        # Occupy the single open-plan slot with a DB plan of this instance.
        async with rig.db.session() as session:
            session.add(TradePlan(
                underlying="SPY", strategy="capped", strategy_id=row["id"],
                legs=[{"symbol": "SPY261218C00800000", "side": 1, "ratio": 1}],
                qty=1, entry_limit=2.0, tp_premium=4.0, sl_premium=1.0,
                time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
                status="filled",
            ))
            await session.commit()
        with pytest.raises(ValueError, match="open plans"):
            await rig.runner.execute_intent(row["id"], option_intent())
        assert rig.trade.placed == []

    @pytest.mark.asyncio
    async def test_flatten_pauses_and_closes_its_plans(self, rig):
        row = await rig.runner.create("dummy", "flat", {})
        await rig.runner.set_state(row["id"], "enabled")
        async with rig.db.session() as session:
            session.add(TradePlan(
                id="p-flat-1", underlying="SPY", strategy="flat",
                strategy_id=row["id"],
                legs=[{"symbol": "SPY261218C00800000", "side": 1, "ratio": 1}],
                qty=1, entry_limit=2.0, tp_premium=4.0, sl_premium=1.0,
                time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
                status="filled",
            ))
            await session.commit()
        result = await rig.runner.flatten(row["id"])
        assert result["closed"] == 1
        assert rig.trade.closed == ["p-flat-1"]
        assert (await rig.runner.get(row["id"]))["state"] == "paused"


class TestCrashIsolation:
    @pytest.mark.asyncio
    async def test_crashing_event_journals_and_loop_survives(self, rig):
        row = await rig.runner.create("dummy", "crashy", {})
        await rig.runner.set_state(row["id"], "enabled")
        strategy = rig.runner._running[row["id"]].strategy

        rig.bus.publish(news_event(boom=True))
        assert await wait_for(lambda: len(strategy.events) == 1)
        rig.bus.publish(news_event(fine=True))  # loop must still be alive
        assert await wait_for(lambda: len(strategy.events) == 2)
        assert "error" in await actions_once(rig.db, row["id"], lambda a: "error" in a)

    @pytest.mark.asyncio
    async def test_sibling_unaffected_by_crash(self, rig):
        row_a = await rig.runner.create("dummy", "boomer", {})
        row_b = await rig.runner.create("dummy", "sibling", {})
        await rig.runner.set_state(row_a["id"], "enabled")
        await rig.runner.set_state(row_b["id"], "enabled")
        strat_b = rig.runner._running[row_b["id"]].strategy

        rig.bus.publish(news_event(boom=True))
        rig.bus.publish(news_event(after=True))
        assert await wait_for(lambda: len(strat_b.events) == 2)

    @pytest.mark.asyncio
    async def test_kill_all_pauses_everything(self, rig):
        for name in ("k1", "k2"):
            row = await rig.runner.create("dummy", name, {})
            await rig.runner.set_state(row["id"], "enabled")
        result = await rig.runner.kill_all()
        assert result["paused"] == 2
        assert rig.runner._running == {}
