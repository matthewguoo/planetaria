"""Equity (shares) support: multiplier semantics, order construction,
risk guards, and the 24/5 session policy. Broker behavior these encode was
verified against the paper API on 2026-08-04 (scripts/verify_equity_paths.py):
extended-hours DAY limits fill premarket/postmarket/overnight; after-hours
market orders queue silently (never submit one); no position_intent needed."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from app.models.trade import TradePlan
from app.services.market_clock import equity_session
from app.services.trade_service import TradeService

ET = ZoneInfo("America/New_York")


def equity_plan(**overrides) -> TradePlan:
    base = dict(
        id="eqplan1", underlying="SPY", strategy="ref", asset_class="equity",
        extended_hours=True,
        legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 700.0}],
        qty=2, entry_limit=700.0, tp_premium=703.0, sl_premium=696.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=6),
        status="filled", fill_premium=700.0, filled_qty=2,
    )
    base.update(overrides)
    return TradePlan(**base)


def option_plan(**overrides) -> TradePlan:
    base = dict(
        id="opplan1", underlying="SPY", strategy="long_call",
        legs=[{"symbol": "SPY261218C00800000", "right": "C", "strike": 800.0,
               "expiry": "2026-12-18", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2}],
        qty=2, entry_limit=2.0, tp_premium=4.0, sl_premium=1.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        status="filled", fill_premium=2.0, filled_qty=2,
    )
    base.update(overrides)
    return TradePlan(**base)


class TestMultiplier:
    def test_pnl_at_x1_for_shares_x100_for_options(self):
        eq = equity_plan()
        assert eq.contract_multiplier == 1
        assert eq.pnl_at(701.5) == pytest.approx(3.0)  # (701.5-700)*1*2

        op = option_plan()
        assert op.contract_multiplier == 100
        assert op.pnl_at(3.5) == pytest.approx(300.0)  # (3.5-2)*100*2

    def test_default_asset_class_is_option(self):
        plan = option_plan()
        assert plan.asset_class in (None, "option")  # column default pre-commit
        assert plan.contract_multiplier == 100


class _CaptureAlpaca:
    configured = True

    def __init__(self):
        self.submitted = []
        self.trading = SimpleNamespace(submit_order=self._submit)

    def _submit(self, request):
        self.submitted.append(request)
        return SimpleNamespace(id="order-1", status="accepted")

    async def call(self, fn, /, *args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("retries", None)
        return fn(*args, **kwargs)


def make_trade_service() -> tuple[TradeService, _CaptureAlpaca]:
    alpaca = _CaptureAlpaca()
    market = SimpleNamespace(broadcast=SimpleNamespace(publish=lambda *a, **k: None))
    trade = TradeService(db=None, alpaca=alpaca, market=market, risk=None)
    return trade, alpaca


class TestOrderConstruction:
    @pytest.mark.asyncio
    async def test_equity_entry_extended_hours_no_position_intent(self):
        trade, alpaca = make_trade_service()
        plan = equity_plan()
        await trade._submit_entry(plan)
        request = alpaca.submitted[0]
        assert isinstance(request, LimitOrderRequest)
        assert request.symbol == "SPY"
        assert request.qty == 2
        assert request.extended_hours is True
        assert request.position_intent is None
        assert request.limit_price == pytest.approx(700.0)
        assert str(request.time_in_force).lower().endswith("day")

    def test_equity_close_limit_carries_extended_hours(self):
        trade, _ = make_trade_service()
        plan = equity_plan()
        request = trade._close_request(plan, 703.0, "cid")
        assert isinstance(request, LimitOrderRequest)
        assert request.extended_hours is True
        assert request.position_intent is None
        assert str(request.side).lower().endswith("sell")

        market_close = trade._close_request(plan, None, "cid2")
        assert isinstance(market_close, MarketOrderRequest)

    def test_option_paths_unchanged(self):
        trade, _ = make_trade_service()
        plan = option_plan()
        request = trade._close_request(plan, 4.0, "cid")
        assert isinstance(request, LimitOrderRequest)
        assert request.position_intent is not None
        assert getattr(request, "extended_hours", None) in (None, False)


class TestEquitySession:
    def at(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=ET)

    def test_weekday_sessions(self):
        assert equity_session(self.at("2026-08-04T02:00")) == "overnight"
        assert equity_session(self.at("2026-08-04T05:00")) == "premarket"
        assert equity_session(self.at("2026-08-04T10:00")) == "rth"
        assert equity_session(self.at("2026-08-04T17:00")) == "postmarket"
        assert equity_session(self.at("2026-08-04T21:00")) == "overnight"

    def test_weekend_gap(self):
        assert equity_session(self.at("2026-08-07T21:00")) is None      # Fri night
        assert equity_session(self.at("2026-08-08T12:00")) is None      # Saturday
        assert equity_session(self.at("2026-08-09T12:00")) is None      # Sunday day
        assert equity_session(self.at("2026-08-09T20:30")) == "overnight"  # Sun open


class TestSessionPolicy:
    """_session_open and _rung_limit against a stubbed RTH clock."""

    def make_enforcer(self, rth_open: bool):
        from app.services.exit_enforcer import ExitEnforcer

        class _Clock:
            async def is_open(self):
                return rth_open

        trade = SimpleNamespace(alpaca=SimpleNamespace(configured=True))
        market = SimpleNamespace()
        enforcer = ExitEnforcer.__new__(ExitEnforcer)  # skip __init__ wiring
        enforcer.trade = trade
        enforcer.market = market
        enforcer.clock = _Clock()
        return enforcer

    @pytest.mark.asyncio
    async def test_equity_extended_open_when_overnight(self, monkeypatch):
        import app.services.exit_enforcer as ee

        enforcer = self.make_enforcer(rth_open=False)
        monkeypatch.setattr(ee, "equity_session", lambda: "overnight")
        assert await enforcer._session_open(equity_plan()) is True
        # Options still follow the RTH clock.
        assert await enforcer._session_open(option_plan()) is False

    @pytest.mark.asyncio
    async def test_equity_weekend_parks(self, monkeypatch):
        import app.services.exit_enforcer as ee

        enforcer = self.make_enforcer(rth_open=False)
        monkeypatch.setattr(ee, "equity_session", lambda: None)
        assert await enforcer._session_open(equity_plan()) is False

    @pytest.mark.asyncio
    async def test_market_rung_substituted_outside_rth(self):
        enforcer = self.make_enforcer(rth_open=False)
        limit = await enforcer._rung_limit(equity_plan(), 700.0, None)
        assert limit == pytest.approx(700.0 * 0.90)  # aggressive limit, never market

        enforcer_rth = self.make_enforcer(rth_open=True)
        assert await enforcer_rth._rung_limit(equity_plan(), 700.0, None) is None
        assert await enforcer_rth._rung_limit(option_plan(), 2.0, None) is None


@pytest.mark.asyncio
class TestEquityRiskGuards:
    async def make_risk(self, tmp_path, name="risk.db"):
        from app.db.session import Database
        from app.services.risk import RiskService

        db = Database()
        await db.connect(f"sqlite+aiosqlite:///{tmp_path}/{name}")
        return RiskService(db), db

    async def test_equity_guards_no_option_keyerror(self, tmp_path):
        risk, db = await self.make_risk(tmp_path)
        violations = await risk.validate_new_trade(
            account_equity=100_000.0,
            entry_cost_dollars=1400.0,
            max_loss_dollars=8.0,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(days=2),
            expiry_date_et="",
            legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 700.0}],
            underlying="SPY",
            asset_class="equity",
        )
        assert violations == []  # incl. no cutoff violation for a 2-day stop
        await db.close()

    async def test_equity_long_only_and_caps(self, tmp_path):
        risk, db = await self.make_risk(tmp_path)
        violations = await risk.validate_new_trade(
            account_equity=100_000.0,
            entry_cost_dollars=6000.0,  # > 5% per-name cap
            max_loss_dollars=10.0,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(days=1),
            expiry_date_et="",
            legs=[{"symbol": "SPY", "side": -1, "ratio": 1, "entry": 700.0}],
            underlying="SPY",
            asset_class="equity",
        )
        text = " ".join(violations)
        assert "shorting is disabled" in text
        assert "per-name cap" in text
        await db.close()

    async def test_equity_gross_exposure_counts_open_equity_plans(self, tmp_path):
        risk, db = await self.make_risk(tmp_path)
        async with db.session() as session:
            session.add(equity_plan(id="open1", qty=70, filled_qty=70))  # $49k open
            await session.commit()
        violations = await risk.validate_new_trade(
            account_equity=100_000.0,
            entry_cost_dollars=2000.0,  # 49k + 2k > 50% cap
            max_loss_dollars=10.0,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(days=1),
            expiry_date_et="",
            legs=[{"symbol": "AMD", "side": 1, "ratio": 1, "entry": 200.0}],
            underlying="AMD",
            asset_class="equity",
        )
        assert any("gross exposure" in v for v in violations)
        await db.close()
