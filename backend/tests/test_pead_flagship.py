"""The flagship's four load-bearing rules, tested as rules rather than as
plumbing: the direction policy, the conditional exit, the slot allocator, and
the liquidity floor.

Each of these is a line in Table 1 of the paper that the study argues for
against a measured alternative, so each is worth a test that would fail if
someone quietly reverted it.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.risk import RiskService
from app.services.signals.events import Event, EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.strategies.pead_flagship import (
    GUIDANCE_MOVED,
    PeadFlagship,
    _exit_time,
    _side,
)

ET_TZ = __import__("zoneinfo").ZoneInfo("America/New_York")


class TestDirectionPolicy:
    """§5.4. Agreement trades with the tape; EVERYTHING else trades against
    it. The paper's central finding is that the gate's refusals are not an
    absence of information — the refused third returns strongly negative, so
    fading them is what the verdicts are for."""

    def test_agreement_trades_with_the_tape(self):
        assert _side("bullish", +6.0, True)[0] == 1
        assert _side("bearish", -6.0, True)[0] == -1

    def test_disagreement_fades_the_tape(self):
        # Bullish read into a falling tape. Fading a DOWN tape means going
        # long, so the sign is the opposite of the move, not of the verdict.
        side, rule = _side("bullish", -6.0, True)
        assert side == 1, "tape is down, fading it means going LONG"
        assert "against the tape" in rule

    def test_neutral_fades_too(self):
        """The single strongest row in the mutation table: +77.3bp on 296
        events. A neutral verdict is not an abstention."""
        assert _side("neutral", +6.0, True)[0] == -1
        assert _side("neutral", -6.0, True)[0] == 1

    def test_never_stand_down_false_degrades_to_the_paper_s_gate(self):
        assert _side("bullish", +6.0, False)[0] == 1     # agreement unchanged
        assert _side("bullish", -6.0, False)[0] == 0     # now stands down
        assert _side("neutral", +6.0, False)[0] == 0


class TestConditionalExit:
    """§5.3 crossed with §5.4: hold to the third session where the verdict
    reports guidance raised or lowered, the next close otherwise. Keys off a
    verdict field known at entry — never off the outcome."""

    def test_guidance_values_that_buy_the_longer_hold(self):
        assert set(GUIDANCE_MOVED) == {"raised", "lowered"}
        assert "maintained" not in GUIDANCE_MOVED
        assert "none_given" not in GUIDANCE_MOVED

    def test_exit_skips_weekends(self):
        friday = datetime(2026, 8, 7, 16, 30, tzinfo=ET_TZ)
        assert friday.weekday() == 4
        t1 = _exit_time(1, friday).astimezone(ET_TZ)
        assert t1.weekday() == 0 and t1.day == 10          # Monday
        t3 = _exit_time(3, friday).astimezone(ET_TZ)
        assert t3.weekday() == 2 and t3.day == 12          # Wednesday

    def test_the_longer_hold_is_actually_longer(self):
        now = datetime(2026, 8, 5, 16, 30, tzinfo=ET_TZ)   # a Wednesday
        assert _exit_time(3, now) > _exit_time(1, now)


class TestParams:
    def test_defaults_are_the_paper_s_table_1(self):
        p = PeadFlagship.default_params
        assert p["n_names"] == 5                 # top 5 per night, §3
        assert p["min_dv_musd"] == 100.0         # the version the paper runs
        assert p["min_move_pct"] == 5.0          # the event gate, §2.1
        assert p["never_stand_down"] is True     # §5.4
        assert p["slots"] == 6                   # §5.5
        assert p["hold_days"] == 1               # §5.3
        assert p["hold_days_guidance"] == 3
        assert p["sl_pct"] is None and p["tp_pct"] is None   # no bracket
        assert p["live"] is False                # note-mode until a fill exists

    def test_declares_what_it_needs(self):
        assert PeadFlagship.requires == frozenset({"sip", "shorts", "llm"})

    def test_the_bracket_is_all_or_nothing(self):
        """One leg is neither a target nor a stop, and place_trade refuses it
        anyway — fail here with the reason instead of at the broker."""
        with pytest.raises(ValueError, match="both"):
            PeadFlagship.validate_params({"sl_pct": 0.2})
        ok = PeadFlagship.validate_params({"sl_pct": 0.2, "tp_pct": 0.4})
        assert ok["sl_pct"] == 0.2 and ok["tp_pct"] == 0.4

    def test_rejects_impossible_slots_and_holds(self):
        with pytest.raises(ValueError, match="slots"):
            PeadFlagship.validate_params({"slots": 0})
        with pytest.raises(ValueError, match="hold_days"):
            PeadFlagship.validate_params({"hold_days": 0})


# --------------------------------------------------------------- allocation


class _Trade:
    """Minimal broker stand-in: a fixed account and a place_trade that
    records rather than sends."""

    def __init__(self, equity: float):
        self._equity = equity
        self.placed: list[dict] = []

    async def get_account(self) -> dict:
        return {"equity": self._equity, "cash": self._equity,
                "buying_power": self._equity * 4}

    async def place_trade(self, payload: dict) -> dict:
        self.placed.append(payload)
        return {"id": f"plan{len(self.placed)}"}


@pytest_asyncio.fixture
async def runner(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/alloc.db")
    r = StrategyRunner(
        db, EventBus(), SignalStore(db), trade=_Trade(100_000.0),
        risk=RiskService(db), market=None, clock=None,
        settings=SimpleNamespace(strategies_enabled=True,
                                 alpaca_stock_feed="iex", llm_backend="none"),
    )
    yield r
    await r.shutdown()
    await db.close()


class TestAllocation:
    """The operator's answer to 'how much of the account does this strategy
    get', enforced rather than advertised."""

    @pytest.mark.asyncio
    async def test_default_is_ten_percent_not_the_whole_account(self, runner):
        """An instance nobody has allocated must not inherit everything by
        omission."""
        row = await runner.create("pead_flagship", "flag", {})
        state = await runner.allocation_state(row["id"])
        assert state["allocation"] == {"mode": "pct", "value": 10.0}
        assert state["allocated"] == 10_000.0
        assert state["available"] == 10_000.0

    @pytest.mark.asyncio
    async def test_percent_and_dollars_both_resolve(self, runner):
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "pct", "value": 25})
        assert (await runner.allocation_state(row["id"]))["allocated"] == 25_000.0
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 7_500})
        assert (await runner.allocation_state(row["id"]))["allocated"] == 7_500.0

    @pytest.mark.asyncio
    async def test_a_dollar_allocation_cannot_exceed_the_account(self, runner):
        """Asking for more than exists is a typo, not capital."""
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000_000})
        assert (await runner.allocation_state(row["id"]))["allocated"] == 100_000.0

    @pytest.mark.asyncio
    async def test_ctx_account_is_scoped_but_still_tells_the_truth(self, runner):
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "pct", "value": 30})
        book = await runner.account(row["id"])
        assert book["equity"] == 30_000.0          # what it may size against
        assert book["account_equity"] == 100_000.0  # the truth, still readable
        assert book["available"] == 30_000.0

    @pytest.mark.asyncio
    async def test_an_intent_past_the_allocation_is_refused(self, runner):
        """The teeth. ctx.account() already told the strategy its `available`;
        this refuses the order if it sized past it anyway."""
        from app.strategies.base import TradeIntent

        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 5_000})
        intent = TradeIntent(
            asset_class="equity", underlying="AMD",
            legs=[{"symbol": "AMD", "side": 1, "ratio": 1, "entry": 100.0}],
            qty=100, entry_limit=100.0, tp=200.0, sl=80.0,   # $10,000 notional
            time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=20),
        )
        with pytest.raises(ValueError, match="allocation"):
            await runner.execute_intent(row["id"], intent)
        assert runner.trade.placed == []

        # Inside the envelope it goes through.
        intent.qty = 40                                       # $4,000
        await runner.execute_intent(row["id"], intent)
        assert len(runner.trade.placed) == 1


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_reports_the_gap_rather_than_hiding_it(self, runner):
        """The engine journalling 'no fresh quote' all night while the console
        shows green is the failure this exists to prevent."""
        caps = await runner.capability_state()
        assert caps["sip"]["met"] is False          # settings say iex
        assert "iex" in caps["sip"]["detail"]
        assert caps["shorts"]["met"] is False       # equity_long_only default
        assert caps["llm"]["met"] is False          # no backend in this fixture

    @pytest.mark.asyncio
    async def test_unmet_requirements_do_not_block_the_instance(self, runner):
        """A shadow run against an unmet requirement is how you find out what
        the requirement costs, so enabling must still work."""
        row = await runner.create("pead_flagship", "flag", {})
        out = await runner.set_state(row["id"], "enabled")
        assert out["state"] == "enabled"


class TestManualIsNotATradingPath:
    @pytest.mark.asyncio
    async def test_an_empty_command_is_journalled_as_unusable(self):
        """The old TRIGGER button posted `{}`, which does nothing for an
        autonomous strategy. It should say so rather than look like it
        worked."""
        strategy = PeadFlagship()
        notes: list[dict] = []

        ctx = SimpleNamespace(
            params=PeadFlagship.default_params,
            note=lambda detail, signal_ids=(): notes.append(detail),
            log=SimpleNamespace(info=lambda *a, **k: None),
        )

        async def note(detail, signal_ids=()):
            notes.append(detail)

        ctx.note = note
        await strategy._manual(
            Event(type="manual", ts=datetime.now(timezone.utc), source="api-trigger",
                  key="k", symbols=(), payload={}), ctx)
        assert "build_watchlist" in notes[0]["skip"]


class TestCircuitBreaker:
    """What replaced the per-position stop. A stop bounds one trade and costs
    edge on every trade; a breaker bounds the strategy and costs nothing until
    it fires."""

    async def _closed(self, runner, name: str, pnls: list[float]) -> None:
        from sqlalchemy import select

        from app.models.strategies import StrategyInstanceRow
        from app.models.trade import TradePlan

        async with runner.db.session() as session:
            sid = await session.scalar(
                select(StrategyInstanceRow.id)
                .where(StrategyInstanceRow.name == name))
            for i, pnl in enumerate(pnls):
                session.add(TradePlan(
                    underlying="AMD", strategy=name, strategy_id=sid,
                    legs=[{"symbol": "AMD", "side": 1, "ratio": 1, "entry": 100.0}],
                    qty=1, entry_limit=100.0, tp_premium=None, sl_premium=None,
                    time_stop_utc=datetime.now(timezone.utc) + timedelta(days=1),
                    status="closed", realized_pnl=pnl,
                    exited_at=datetime.now(timezone.utc) + timedelta(minutes=i),
                ))
            await session.commit()

    @pytest.mark.asyncio
    async def test_drawdown_is_measured_from_the_high_water_mark(self, runner):
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        # +1000 (peak) then -400: the drawdown is 400, not the 400 net loss
        # from zero and not the -400 trade alone.
        await self._closed(runner, "flag", [1_000.0, -400.0])
        state = await runner.breaker_state(row["id"])
        assert state["realized"] == 600.0
        assert state["peak"] == 1_000.0
        assert state["drawdown"] == 400.0

    @pytest.mark.asyncio
    async def test_a_recovered_strategy_is_not_still_tripped(self, runner):
        """Tripping on the worst-ever drawdown would mean a strategy that
        dipped and recovered stays tripped for good."""
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await runner.set_breaker(row["id"], {"mode": "usd", "value": 300})
        await self._closed(runner, "flag", [1_000.0, -400.0, 500.0])
        state = await runner.breaker_state(row["id"])
        assert state["drawdown"] == 0.0          # new high-water mark
        assert state["max_drawdown"] == 400.0    # reported, not acted on
        assert state["tripped"] is False

    @pytest.mark.asyncio
    async def test_pct_is_of_the_allocation_not_the_account(self, runner):
        """A breaker must not silently loosen because the account grew."""
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await runner.set_breaker(row["id"], {"mode": "pct", "value": 20})
        assert (await runner.breaker_state(row["id"]))["limit"] == 2_000.0

    @pytest.mark.asyncio
    async def test_tripping_flattens_and_pauses(self, runner):
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await runner.set_breaker(row["id"], {"mode": "usd", "value": 500})
        await runner.set_state(row["id"], "enabled")
        await self._closed(runner, "flag", [-600.0])

        tripped = await runner.check_breakers()
        assert [t["name"] for t in tripped] == ["flag"]
        assert (await runner.get(row["id"]))["state"] == "paused"
        # And it says why, in the journal, rather than just going quiet.
        why = [d for d in await runner.decisions(row["id"])
               if (d.get("detail") or {}).get("circuit_breaker")]
        assert why and "drawdown" in why[0]["detail"]["why"]

    @pytest.mark.asyncio
    async def test_a_tripped_strategy_cannot_sneak_one_more_trade_in(self, runner):
        """Checked before every intent, not only on the sweep."""
        from app.strategies.base import TradeIntent

        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await runner.set_breaker(row["id"], {"mode": "usd", "value": 500})
        await self._closed(runner, "flag", [-600.0])
        intent = TradeIntent(
            asset_class="equity", underlying="AMD",
            legs=[{"symbol": "AMD", "side": 1, "ratio": 1, "entry": 100.0}],
            qty=1, entry_limit=100.0, tp=None, sl=None,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=20),
        )
        with pytest.raises(ValueError, match="circuit breaker"):
            await runner.execute_intent(row["id"], intent)
        assert runner.trade.placed == []

    @pytest.mark.asyncio
    async def test_disabled_breaker_never_trips(self, runner):
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await runner.set_breaker(row["id"], {"enabled": False, "mode": "usd",
                                             "value": 1})
        await self._closed(runner, "flag", [-9_000.0])
        assert (await runner.breaker_state(row["id"]))["tripped"] is False


class _Market:
    """latest_quote stand-in: symbol -> quote dict."""

    def __init__(self, quotes: dict[str, dict]):
        self._quotes = quotes

    def latest_quote(self, symbol: str) -> dict | None:
        return self._quotes.get(symbol)


class TestBreakerMarksOpenPositions:
    """The unrealised leg of the breaker. Marking is per LEG through pnl_at:
    on 2026-09-01 the old code marked fly-1's freshly filled one-set iron fly
    at ITS UNDERLYING'S price — a $70,648 phantom loss on a $545-max-loss
    structure — tripped the $2,000 breaker and flattened a good fill."""

    FLY_LEGS = [
        {"symbol": "QQQ260901C00708000", "right": "C", "side": -1, "ratio": 1},
        {"symbol": "QQQ260901P00708000", "right": "P", "side": -1, "ratio": 1},
        {"symbol": "QQQ260901C00715000", "right": "C", "side": 1, "ratio": 1},
        {"symbol": "QQQ260901P00701000", "right": "P", "side": 1, "ratio": 1},
    ]

    async def _open_plan(self, runner, name: str, **fields) -> None:
        from sqlalchemy import select

        from app.models.strategies import StrategyInstanceRow
        from app.models.trade import TradePlan

        async with runner.db.session() as session:
            sid = await session.scalar(
                select(StrategyInstanceRow.id)
                .where(StrategyInstanceRow.name == name))
            session.add(TradePlan(
                strategy=name, strategy_id=sid, status="filled",
                tp_premium=None, sl_premium=None,
                time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
                **fields,
            ))
            await session.commit()

    async def _fly(self, runner) -> str:
        """fly-1's actual 2026-09-01 plan: short 708 straddle, long wings,
        filled at the $1.55 credit."""
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await runner.set_breaker(row["id"], {"mode": "pct", "value": 20})
        await self._open_plan(
            runner, "flag", underlying="QQQ", asset_class="option",
            legs=self.FLY_LEGS, qty=1, filled_qty=1,
            entry_limit=-1.55, fill_premium=-1.55,
        )
        return row["id"]

    @pytest.mark.asyncio
    async def test_an_option_structure_is_marked_at_its_legs_not_its_underlying(self, runner):
        row_id = await self._fly(runner)
        runner.market = _Market({
            "QQQ": {"bid": 708.06, "ask": 708.08, "mid": 708.07},
            "QQQ260901C00708000": {"bid": 0.82, "ask": 0.88, "mid": 0.85},
            "QQQ260901P00708000": {"bid": 0.73, "ask": 0.79, "mid": 0.76},
            "QQQ260901C00715000": {"bid": 0.01, "ask": 0.04, "mid": 0.025},
            "QQQ260901P00701000": {"bid": 0.01, "ask": 0.02, "mid": 0.015},
        })
        state = await runner.breaker_state(row_id)
        # Structure mid -1.57 vs the -1.55 fill: down $2 on the set.
        assert state["unrealized"] == -2.0
        assert state["tripped"] is False

    @pytest.mark.asyncio
    async def test_an_unquoted_leg_marks_the_plan_at_entry(self, runner):
        """Only the underlying quoted — exactly the moment after fly-1's fill.
        Zero, not spot x 100."""
        row_id = await self._fly(runner)
        runner.market = _Market({"QQQ": {"bid": 708.06, "ask": 708.08,
                                         "mid": 708.07}})
        state = await runner.breaker_state(row_id)
        assert state["unrealized"] == 0.0
        assert state["tripped"] is False

    @pytest.mark.asyncio
    async def test_a_short_equity_plan_still_marks_signed(self, runner):
        row = await runner.create("pead_flagship", "flag", {})
        await runner.set_allocation(row["id"], {"mode": "usd", "value": 10_000})
        await self._open_plan(
            runner, "flag", underlying="AMD", asset_class="equity",
            legs=[{"symbol": "AMD", "side": -1, "ratio": 1, "entry": 100.0}],
            qty=10, filled_qty=10, entry_limit=-100.0, fill_premium=-100.0,
        )
        runner.market = _Market({"AMD": {"bid": 94.9, "ask": 95.1, "mid": 95.0}})
        state = await runner.breaker_state(row["id"])
        assert state["unrealized"] == 50.0    # short, tape fell $5 x 10 shares


class TestDelete:
    @pytest.mark.asyncio
    async def test_removes_the_row_and_its_journal(self, runner):
        row = await runner.create("pead_flagship", "gone", {})
        await runner.journal_note(row["id"], {"note": "something"})
        out = await runner.delete(row["id"])
        assert out["decisions_deleted"] == 1
        with pytest.raises(ValueError):
            await runner.get(row["id"])

    @pytest.mark.asyncio
    async def test_refuses_while_plans_are_open(self, runner):
        """Deleting the row would strand positions whose strategy_id no
        longer resolves to anything."""
        from app.models.trade import TradePlan

        row = await runner.create("pead_flagship", "held", {})
        async with runner.db.session() as session:
            session.add(TradePlan(
                underlying="AMD", strategy="held", strategy_id=row["id"],
                legs=[{"symbol": "AMD", "side": 1, "ratio": 1, "entry": 100.0}],
                qty=1, entry_limit=100.0, tp_premium=None, sl_premium=None,
                time_stop_utc=datetime.now(timezone.utc) + timedelta(days=1),
                status="filled",
            ))
            await session.commit()
        with pytest.raises(ValueError, match="flatten it first"):
            await runner.delete(row["id"])


class TestReaderParams:
    """Which model reads the release, and how hard. The paper's numbers are
    one model at one effort; a silent upgrade underneath them is a different
    experiment, so both are pinnable per instance."""

    def test_model_defaults_to_the_engine_and_can_be_pinned(self):
        assert PeadFlagship.default_params["model"] is None
        pinned = PeadFlagship.validate_params({"model": "claude-opus-5"})
        assert pinned["model"] == "claude-opus-5"

    def test_effort_is_checked_against_what_the_gateway_understands(self):
        assert PeadFlagship.default_params["effort"] == "medium"
        with pytest.raises(ValueError, match="effort"):
            PeadFlagship.validate_params({"effort": "maximum"})
        assert PeadFlagship.validate_params({"effort": "high"})["effort"] == "high"

    def test_blank_model_is_refused_rather_than_silently_defaulting(self):
        with pytest.raises(ValueError, match="model"):
            PeadFlagship.validate_params({"model": "   "})
