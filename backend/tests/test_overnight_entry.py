"""Overnight (Blue Ocean) equity ENTRY path.

The 2026-08-04 e2e run found two blockers during the 20:00-04:00 ET session:
(1) place_trade gated on the GLOBAL stream age — the IEX stream is rightly
silent overnight, so it read infinite and every equity entry was refused;
(2) REST latest-quote served the dead 17:00 book (791.22 ask vs 772.93 on
the live tape), so strategies had nothing sane to price off. These tests
cover the fixes: per-symbol tape freshness feeding the risk gate, and
market.overnight_price() consulting the tape the overnight poller rolls.
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.market_data as md
import app.services.trade_service as ts
from app.services.bar_store import BarStore
from app.services.market_data import MarketDataService
from app.services.trade_service import TradeService

def ms_ago(seconds: float) -> float:
    return (time.time() - seconds) * 1000


class FakeRedis:
    healthy = False

    async def save_bars(self, *a, **k):
        pass

    async def load_bars(self, *a, **k):
        return {}

    async def trim_bars(self, *a, **k):
        pass


class FakeAlpaca:
    def __init__(self, configured=True):
        self.configured = configured

    async def call(self, fn, /, *args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("retries", None)
        return fn(*args, **kwargs)


def make_market(configured=True) -> MarketDataService:
    svc = MarketDataService(
        settings=SimpleNamespace(alpaca_api_key="k", alpaca_secret_key="s"),
        alpaca=FakeAlpaca(configured),
        redis=FakeRedis(),
        bars=BarStore(FakeRedis()),
        broadcaster=SimpleNamespace(publish=lambda *a, **k: None),
    )
    svc._stock_stream = SimpleNamespace(
        subscribe_bars=lambda *a: None, subscribe_quotes=lambda *a: None
    )
    svc._backfilled.add("SPY")  # keep subscribe_stock from kicking REST backfill
    return svc


# --------------------------------------------------------- equity_tape_age_s


class TestEquityTapeAge:
    def test_no_evidence_is_none(self):
        assert make_market().equity_tape_age_s("SPY") is None

    def test_quote_age(self):
        svc = make_market()
        svc._latest_quotes["SPY"] = {"mid": 773.0, "ts": ms_ago(12)}
        assert svc.equity_tape_age_s("SPY") == pytest.approx(12, abs=2)

    @pytest.mark.asyncio
    async def test_forming_bar_counts_as_live(self):
        """A bar opened 30s ago whose minute is still forming is live tape —
        its close clamps to now, not to a minute of staleness."""
        svc = make_market()
        svc._latest_quotes["SPY"] = {"mid": 791.0, "ts": ms_ago(4 * 3600)}  # dead book
        now = int(time.time() * 1000)
        bar = {"t": now - now % 60_000, "o": 773, "h": 773, "l": 772.9,
               "c": 772.93, "v": 5}  # the currently-forming minute
        await svc.bars.upsert_1m("SPY", bar)
        assert svc.equity_tape_age_s("SPY") == pytest.approx(0, abs=2)

    @pytest.mark.asyncio
    async def test_old_bar_reads_stale(self):
        svc = make_market()
        t = int(ms_ago(600))
        bar = {"t": t - t % 60_000, "o": 773, "h": 773, "l": 772.9, "c": 772.93, "v": 5}
        await svc.bars.upsert_1m("SPY", bar)
        age = svc.equity_tape_age_s("SPY")
        assert 400 < age < 700  # ~bar close (t+60s) ago


# --------------------------------------------------- _poll_overnight_symbol


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        return FakeResponse(self.payload)


def trade_payload(price=772.93, size=5, age_s=3.0, trade_id=101) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return {"trade": {"p": price, "s": size,
                      "t": ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                      "i": trade_id}}


@pytest.mark.asyncio
class TestOvernightPoll:
    async def test_trade_becomes_quote_and_bar(self):
        svc = make_market()
        client = FakeHttp(trade_payload())
        await svc._poll_overnight_symbol(client, "SPY")

        quote = svc.latest_quote("SPY")
        assert quote["bid"] == quote["ask"] == quote["mid"] == pytest.approx(772.93)
        assert quote["src"] == "overnight"
        bars = svc.bars.get_bars("SPY", "1m")
        assert bars and bars[-1]["c"] == pytest.approx(772.93)
        assert bars[-1]["v"] == 5
        assert "feed=overnight" in client.urls[0]

    async def test_same_trade_id_not_double_counted(self):
        svc = make_market()
        client = FakeHttp(trade_payload())
        await svc._poll_overnight_symbol(client, "SPY")
        await svc._poll_overnight_symbol(client, "SPY")
        assert svc.bars.get_bars("SPY", "1m")[-1]["v"] == 5

    async def test_weekend_stale_print_ignored(self):
        svc = make_market()
        client = FakeHttp(trade_payload(age_s=25 * 60))
        await svc._poll_overnight_symbol(client, "SPY")
        assert svc.latest_quote("SPY") is None
        assert svc.bars.get_bars("SPY", "1m") == []


# --------------------------------------------------------- overnight_price


@pytest.mark.asyncio
class TestOvernightPrice:
    async def test_subscribes_and_polls_tape(self, monkeypatch):
        svc = make_market()
        monkeypatch.setattr(md, "is_overnight_et", lambda now: True)

        async def fake_poll(symbol):  # what _poll_overnight_symbol would do
            svc._latest_quotes[symbol] = {
                "t": "quote", "symbol": symbol, "bid": 772.93, "ask": 772.93,
                "mid": 772.93, "ts": ms_ago(3), "src": "overnight"}

        monkeypatch.setattr(svc, "_poll_overnight_once", fake_poll)
        quote = await svc.overnight_price("spy")
        assert quote["ask"] == pytest.approx(772.93)
        assert quote["src"] == "overnight"
        # The subscription is a BORROW, released before returning: the poll
        # loop iterates _stock_refs, so a lingering entry-pricing ref would
        # pin the symbol into the 10s poller forever on skipped entries. A
        # filled plan's freshness comes from the exit monitor's own ref.
        assert svc._stock_refs.get("SPY", 0) == 0

    async def test_ref_released_even_when_no_evidence(self, monkeypatch):
        """The skip path (no tape at all -> None) must release the borrowed
        ref too — skips are exactly the calls that never reach a monitor,
        so a leak here would accumulate one pinned symbol per scan."""
        svc = make_market()
        monkeypatch.setattr(md, "is_overnight_et", lambda now: True)

        async def quiet_poll(symbol):
            pass

        monkeypatch.setattr(svc, "_poll_overnight_once", quiet_poll)
        for _ in range(3):
            assert await svc.overnight_price("SPY") is None
        assert svc._stock_refs.get("SPY", 0) == 0

    async def test_bar_beats_dead_quote(self, monkeypatch):
        """Restart case: bars rehydrate from Redis, the quote cache doesn't.
        A failed one-shot poll must still price off the freshest bar."""
        svc = make_market()
        monkeypatch.setattr(md, "is_overnight_et", lambda now: True)

        async def failing_poll(symbol):
            raise RuntimeError("REST down")

        monkeypatch.setattr(svc, "_poll_overnight_once", failing_poll)
        svc._latest_quotes["SPY"] = {"mid": 791.06, "bid": 790.9, "ask": 791.22,
                                     "ts": ms_ago(4 * 3600)}  # 17:00 book
        t = int(ms_ago(20))
        await svc.bars.upsert_1m("SPY", {"t": t - t % 60_000, "o": 772.9, "h": 773,
                                         "l": 772.9, "c": 772.93, "v": 3})
        quote = await svc.overnight_price("SPY")
        assert quote["src"] == "overnight-bar"
        assert quote["bid"] == quote["ask"] == pytest.approx(772.93)

    async def test_no_evidence_returns_none(self, monkeypatch):
        svc = make_market()
        monkeypatch.setattr(md, "is_overnight_et", lambda now: True)

        async def quiet_poll(symbol):
            pass

        monkeypatch.setattr(svc, "_poll_overnight_once", quiet_poll)
        assert await svc.overnight_price("SPY") is None

    async def test_outside_overnight_delegates_to_rest_quote(self, monkeypatch):
        svc = make_market()
        monkeypatch.setattr(md, "is_overnight_et", lambda now: False)
        sentinel = {"bid": 700.0, "ask": 700.1, "mid": 700.05, "ts": ms_ago(1)}

        async def fake_fetch(symbol):
            return sentinel

        monkeypatch.setattr(svc, "fetch_latest_stock_quote", fake_fetch)
        assert await svc.overnight_price("SPY") is sentinel
        assert svc._stock_refs == {}  # no overnight subscription outside the session


# ----------------------------------------------------- _entry_staleness_s


def make_trade_service(market) -> TradeService:
    return TradeService(
        db=None, alpaca=SimpleNamespace(configured=True), market=market, risk=None
    )


def stub_market(stream_age=None, tape_age=None):
    return SimpleNamespace(
        broadcast=SimpleNamespace(publish=lambda *a, **k: None),
        stream_age_s=stream_age,
        equity_tape_age_s=lambda symbol: tape_age,
    )


EQUITY_LEGS = [{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 772.93}]


class TestEntryStaleness:
    def test_equity_overnight_uses_symbol_tape(self, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade = make_trade_service(stub_market(stream_age=None, tape_age=4.0))
        assert trade._entry_staleness_s("equity", EQUITY_LEGS) == pytest.approx(4.0)

    def test_equity_overnight_without_tape_blocks(self, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade = make_trade_service(stub_market(stream_age=1.0, tape_age=None))
        assert trade._entry_staleness_s("equity", EQUITY_LEGS) == float("inf")

    def test_equity_in_rth_uses_global_stream(self, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "rth")
        trade = make_trade_service(stub_market(stream_age=1.5, tape_age=9999.0))
        assert trade._entry_staleness_s("equity", EQUITY_LEGS) == pytest.approx(1.5)

    def test_options_always_use_global_stream(self, monkeypatch):
        """Options behavior is byte-identical: a silent stream still reads
        as no-data (inf) even while the equity overnight session is live."""
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade = make_trade_service(stub_market(stream_age=None, tape_age=2.0))
        assert trade._entry_staleness_s("option", EQUITY_LEGS) == float("inf")
        trade2 = make_trade_service(stub_market(stream_age=7.0, tape_age=2.0))
        assert trade2._entry_staleness_s("option", EQUITY_LEGS) == pytest.approx(7.0)


# ------------------------------------------------- place_trade integration


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
    """Overnight world: global stream dead, per-symbol tape fresh (or not)."""

    stream_age_s = None

    def __init__(self, tape_age_s, price=772.93):
        self.broadcast = SimpleNamespace(publish=lambda *a, **k: None)
        self.tape_age_s = tape_age_s
        self.price = price

    def equity_tape_age_s(self, symbol):
        return self.tape_age_s

    def latest_quote(self, symbol):
        return {"bid": self.price, "ask": self.price, "mid": self.price,
                "ts": ms_ago(self.tape_age_s or 0), "src": "overnight"}

    async def fetch_latest_stock_quote(self, symbol):
        return self.latest_quote(symbol)

    def spot(self, symbol):
        return self.price


def equity_payload(price=772.93) -> dict:
    return {
        "underlying": "SPY", "strategy": "ref-spy",
        "asset_class": "equity", "extended_hours": True,
        "legs": [{"symbol": "SPY", "side": 1, "ratio": 1, "entry": price}],
        "qty": 1, "entry_limit": price,
        "tp_premium": round(price * 1.003, 2),
        "sl_premium": round(price * 0.995, 2),
        "time_stop_utc": (datetime.now(timezone.utc)
                          + timedelta(minutes=30)).isoformat(),
    }


def option_payload() -> dict:
    return {
        "underlying": "SPY", "strategy": "long_call",
        "legs": [{"symbol": "SPY261218C00800000", "right": "C", "strike": 800.0,
                  "expiry": "2026-12-18", "side": 1, "ratio": 1,
                  "entry": 2.0, "iv": 0.2}],
        "qty": 1, "entry_limit": 2.0, "tp_premium": 4.0, "sl_premium": 1.0,
        "time_stop_utc": (datetime.now(timezone.utc)
                          + timedelta(minutes=30)).isoformat(),
    }


@pytest.mark.asyncio
class TestPlaceTradeOvernight:
    async def make_rig(self, tmp_path, tape_age_s):
        from app.db.session import Database
        from app.services.risk import RiskService

        db = Database()
        await db.connect(f"sqlite+aiosqlite:///{tmp_path}/overnight.db")
        alpaca = CaptureAlpaca()
        market = TapeMarket(tape_age_s)
        trade = TradeService(db=db, alpaca=alpaca, market=market,
                             risk=RiskService(db))
        return trade, alpaca, db

    async def test_fresh_tape_places_overnight_equity_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.make_rig(tmp_path, tape_age_s=5.0)
        try:
            result = await trade.place_trade(equity_payload())
            assert result["status"] == "submitted"
            request = alpaca.submitted[0]
            assert request.symbol == "SPY"
            assert request.extended_hours is True
            assert request.limit_price == pytest.approx(772.93)
        finally:
            await db.close()

    async def test_dead_tape_still_blocks(self, tmp_path, monkeypatch):
        """No per-symbol evidence => the entry is refused exactly like the
        old global gate would have (never trade blind)."""
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.make_rig(tmp_path, tape_age_s=None)
        try:
            with pytest.raises(ValueError, match="market data stale"):
                await trade.place_trade(equity_payload())
            assert alpaca.submitted == []
        finally:
            await db.close()

    async def test_stale_tape_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.make_rig(tmp_path, tape_age_s=400.0)
        try:
            with pytest.raises(ValueError, match="market data stale"):
                await trade.place_trade(equity_payload())
            assert alpaca.submitted == []
        finally:
            await db.close()

    async def test_option_entry_overnight_still_blocked_by_global_stream(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ts, "equity_session", lambda: "overnight")
        trade, alpaca, db = await self.make_rig(tmp_path, tape_age_s=5.0)
        try:
            with pytest.raises(ValueError, match="market data stale"):
                await trade.place_trade(option_payload())
            assert alpaca.submitted == []
        finally:
            await db.close()
