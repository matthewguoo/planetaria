"""The equity SWING exit contract: SL-without-TP plans (hard stop under an
open-ended winner), the server-side half_spread fill, the auction field's
route passthrough, and bracketless safety in portfolio risk."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.trade_service as ts
from app.api.routes.trading import OrderIn
from app.db.session import Database
from app.models.trade import TradePlan
from app.services.exit_enforcer import _bracket_span
from app.services.portfolio_risk import plan_stop_risk
from app.services.risk import RiskService
from app.services.trade_service import TradeService


def ms_ago(seconds: float) -> float:
    import time

    return (time.time() - seconds) * 1000


class CaptureAlpaca:
    configured = True

    def __init__(self):
        self.submitted = []
        self.trading = SimpleNamespace(
            submit_order=self._submit, get_account=self._account
        )

    def _submit(self, request):
        self.submitted.append(request)
        return SimpleNamespace(id="order-1", status="accepted")

    def _account(self):
        return SimpleNamespace(equity="100000", cash="100000",
                               buying_power="200000", daytrade_count=0,
                               status="ACTIVE")

    async def call(self, fn, /, *args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("retries", None)
        return fn(*args, **kwargs)


class TapeMarket:
    stream_age_s = None

    def __init__(self, price=100.0, half_spread=0.01):
        self.broadcast = SimpleNamespace(publish=lambda *a, **k: None)
        self.price = price
        self.half_spread = half_spread

    def equity_tape_age_s(self, symbol):
        return 2.0

    def latest_quote(self, symbol):
        return {"bid": self.price - self.half_spread,
                "ask": self.price + self.half_spread,
                "mid": self.price, "ts": ms_ago(1), "src": "test"}

    async def fetch_latest_stock_quote(self, symbol):
        return self.latest_quote(symbol)

    def spot(self, symbol):
        return self.price


def sl_only_payload(price=100.0, **overrides) -> dict:
    payload = {
        "underlying": "TQQQ", "strategy": "manual_equity",
        "asset_class": "equity", "extended_hours": True,
        "legs": [{"symbol": "TQQQ", "side": 1, "ratio": 1, "entry": price}],
        "qty": 2, "entry_limit": price,
        "tp_premium": None,
        "sl_premium": round(price * 0.95, 2),
        "time_stop_utc": (datetime.now(timezone.utc)
                          + timedelta(days=20)).isoformat(),
    }
    payload.update(overrides)
    return payload


async def make_rig(tmp_path, market=None):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/swing.db")
    alpaca = CaptureAlpaca()
    market = market or TapeMarket()
    trade = TradeService(db=db, alpaca=alpaca, market=market, risk=RiskService(db))
    return trade, alpaca, db


@pytest.mark.asyncio
class TestSlOnlyEquity:
    async def test_sl_only_swing_plan_places(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.rig(tmp_path)
        try:
            result = await trade.place_trade(sl_only_payload())
            assert result["status"] == "submitted"
            assert result["tp_premium"] is None
            assert result["sl_premium"] == pytest.approx(95.0)
        finally:
            await db.close()

    async def rig(self, tmp_path, market=None):
        return await make_rig(tmp_path, market)

    async def test_sl_only_refused_for_options(self, tmp_path):
        trade, alpaca, db = await self.rig(tmp_path)
        try:
            payload = {
                "underlying": "SPY", "strategy": "long_call",
                "legs": [{"symbol": "SPY261218C00800000", "right": "C",
                          "strike": 800.0, "expiry": "2026-12-18", "side": 1,
                          "ratio": 1, "entry": 2.0, "iv": 0.2}],
                "qty": 1, "entry_limit": 2.0,
                "tp_premium": None, "sl_premium": 1.0,
                "time_stop_utc": (datetime.now(timezone.utc)
                                  + timedelta(minutes=30)).isoformat(),
            }
            with pytest.raises(ValueError, match="equity swing shape"):
                await trade.place_trade(payload)
            assert alpaca.submitted == []
        finally:
            await db.close()

    async def test_tp_without_sl_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.rig(tmp_path)
        try:
            with pytest.raises(ValueError, match="not an exit plan"):
                await trade.place_trade(
                    sl_only_payload(tp_premium=110.0, sl_premium=None)
                )
            assert alpaca.submitted == []
        finally:
            await db.close()

    async def test_missing_time_stop_refused_with_backstop_hint(self, tmp_path):
        trade, alpaca, db = await self.rig(tmp_path)
        try:
            with pytest.raises(ValueError, match="backstop"):
                await trade.place_trade(sl_only_payload(time_stop_utc=None))
            assert alpaca.submitted == []
        finally:
            await db.close()

    async def test_stop_above_entry_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.rig(tmp_path)
        try:
            with pytest.raises(ValueError, match="stop must sit below entry"):
                await trade.place_trade(sl_only_payload(sl_premium=101.0))
        finally:
            await db.close()


@pytest.mark.asyncio
class TestServerSideHalfSpread:
    async def test_wide_book_refused_even_when_client_omits_half_spread(
        self, tmp_path, monkeypatch
    ):
        """The illiquidity gate used to be skippable by omitting the field;
        the server now fills it from the live book."""
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        market = TapeMarket(price=100.0, half_spread=20.0)  # 20% of mid
        trade, alpaca, db = await make_rig(tmp_path, market)
        try:
            with pytest.raises(ValueError, match="illiquid"):
                await trade.place_trade(sl_only_payload())
            assert alpaca.submitted == []
        finally:
            await db.close()


class TestOrderInModel:
    def test_auction_survives_model_dump(self):
        body = OrderIn(
            underlying="SPY", strategy="gff-manual", asset_class="equity",
            legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 640.0,
                   "auction": "open"}],
            qty=1, entry_limit=640.0, tp_premium=None, sl_premium=636.0,
            time_stop_utc=(datetime.now(timezone.utc)
                           + timedelta(hours=7)).isoformat(),
        )
        assert body.model_dump()["legs"][0]["auction"] == "open"

    def test_side_zero_rejected(self):
        with pytest.raises(Exception):
            OrderIn(
                underlying="SPY", strategy="x", asset_class="equity",
                legs=[{"symbol": "SPY", "side": 0, "ratio": 1, "entry": 640.0}],
                qty=1, entry_limit=640.0,
                time_stop_utc=datetime.now(timezone.utc).isoformat(),
            )

    def test_nullable_exits_default_none(self):
        body = OrderIn(
            underlying="SPY", strategy="x", asset_class="equity",
            legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 640.0}],
            qty=1, entry_limit=640.0,
            time_stop_utc=datetime.now(timezone.utc).isoformat(),
        )
        assert body.tp_premium is None and body.sl_premium is None


class TestBracketlessSafety:
    def _plan(self, **over):
        base = dict(
            id="p1", underlying="TQQQ", strategy="manual_equity",
            asset_class="equity",
            legs=[{"symbol": "TQQQ", "side": 1, "ratio": 1, "entry": 100.0}],
            qty=10, entry_limit=100.0, tp_premium=None, sl_premium=None,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(days=5),
            status="filled", fill_premium=100.0, filled_qty=10,
        )
        base.update(over)
        return TradePlan(**base)

    def test_plan_stop_risk_none_sl_is_zero_not_typeerror(self):
        assert plan_stop_risk(self._plan()) == 0.0

    def test_plan_stop_risk_equity_multiplier_is_1(self):
        plan = self._plan(sl_premium=95.0)
        assert plan_stop_risk(plan) == pytest.approx(50.0)  # (100-95)*1*10

    def test_bracket_span_sl_only_uses_entry_to_stop(self):
        plan = self._plan(sl_premium=95.0)
        assert _bracket_span(plan) == pytest.approx(5.0)
        both = self._plan(sl_premium=95.0, tp_premium=110.0)
        assert _bracket_span(both) == pytest.approx(15.0)
        assert _bracket_span(self._plan()) == 1.0
