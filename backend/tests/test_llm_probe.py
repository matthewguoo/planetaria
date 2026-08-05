"""llm_probe through the real runner: the 'strategy as LLM environment' proof.

An instance's params carry the system prompt; a triggered event flows
manual signal -> ctx.analyze() (fake client) -> analysis signal chained to
the trigger -> decision note carrying the verdict with both signal ids.
Also pins the per-class event budget: llm_probe declares 120s and the
runner must honor a class override in both directions.
"""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.llm import LLMAnalyst
from app.services.risk import RiskService
from app.services.signals.events import EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.strategies import REGISTRY, register
from app.strategies.base import Strategy
from tests.test_llm_analyze import FakeAnthropic, ok_response

pytestmark = pytest.mark.asyncio

SETTINGS = SimpleNamespace(strategies_enabled=True, anthropic_api_key="k",
                           llm_model="claude-opus-5", llm_effort="low")


@pytest_asyncio.fixture
async def rig(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/probe.db")
    bus = EventBus()
    store = SignalStore(db)
    fake = FakeAnthropic(ok_response())
    runner = StrategyRunner(
        db, bus, store, trade=None, risk=RiskService(db), market=None,
        clock=None, settings=SETTINGS,
        llm=LLMAnalyst(SETTINGS, store, client=fake),
    )
    yield SimpleNamespace(db=db, bus=bus, store=store, runner=runner, fake=fake)
    await runner.shutdown()
    await db.close()


async def wait_for(predicate, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def trigger(rig, row_id, text):
    """Mimic the /trigger route: journal a manual signal, publish scoped."""
    from datetime import datetime, timezone

    from app.services.signals.events import Event

    event = Event(type="manual", ts=datetime.now(timezone.utc), source="api",
                  key=None, symbols=(), payload={"strategy_id": row_id, "text": text})
    journaled, _ = await rig.store.record(event)
    rig.bus.publish(journaled)
    return journaled


class TestLLMProbe:
    async def test_trigger_to_verdict_with_provenance(self, rig):
        row = await rig.runner.create("llm_probe", "probe", {
            "system": "You are a test analyst.",
        })
        await rig.runner.set_state(row["id"], "enabled")

        journaled = await trigger(rig, row["id"], "ACME guides revenue down 20%")

        assert await wait_for(lambda: rig.fake.calls)
        # The instance's system prompt param reached the model call.
        assert "You are a test analyst." in rig.fake.calls[0]["system"]

        # Poll for the note decision carrying the verdict.
        deadline = asyncio.get_event_loop().time() + 3.0
        note = None
        while asyncio.get_event_loop().time() < deadline:
            rows = await rig.runner.decisions(row["id"])
            note = next((r for r in rows if r["action"] == "note"
                         and "verdict" in (r["detail"] or {})), None)
            if note:
                break
            await asyncio.sleep(0.02)
        assert note, "no verdict note journaled"
        assert note["detail"]["verdict"]["direction"] == "bearish"

        # Provenance: the note references BOTH the trigger signal and the
        # analysis signal, and the analysis row chains to the trigger.
        analysis_rows = await rig.store.recent(type="analysis")
        assert len(analysis_rows) == 1
        assert analysis_rows[0]["parent_id"] == journaled.signal_id
        assert set(note["signal_ids"]) == {journaled.signal_id,
                                           analysis_rows[0]["id"]}

    async def test_catalog_exposes_llm_probe(self, rig):
        assert "llm_probe" in REGISTRY
        assert REGISTRY["llm_probe"].event_timeout_s == 120.0


class TestPerClassEventTimeout:
    async def test_short_budget_class_times_out(self, rig):
        class Slowpoke(Strategy):
            """Sleeps past its own tiny budget."""

            kind = "slowpoke"
            subscriptions = ("manual",)
            event_timeout_s = 0.05
            default_params = {}

            async def on_event(self, event, ctx):
                await asyncio.sleep(0.5)

        register(Slowpoke)
        try:
            row = await rig.runner.create("slowpoke", "slow", {})
            await rig.runner.set_state(row["id"], "enabled")
            await trigger(rig, row["id"], "x")

            deadline = asyncio.get_event_loop().time() + 3.0
            err = None
            while asyncio.get_event_loop().time() < deadline:
                rows = await rig.runner.decisions(row["id"])
                err = next((r for r in rows if r["action"] == "error"), None)
                if err:
                    break
                await asyncio.sleep(0.02)
            assert err and "timeout after 0s" in err["detail"]["error"]
        finally:
            REGISTRY.pop("slowpoke", None)
