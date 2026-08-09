"""The no-SIP variant's three load-bearing deltas, tested as rules:

1. It is the flagship except the pricing seam — policy params identical,
   `requires` drops "sip" and nothing else.
2. The pricing seam consults the multi-source AH path with the configured
   staleness budget.
3. Every sized decision buys a delayed-SIP truth check `verify_min` minutes
   later, in note-mode and order-mode alike — and the check's marketability
   arithmetic is right for both sides, because that journal is the AMC fill
   simulation the whole variant exists to produce.
"""

import logging
import time as _time
from datetime import datetime, timezone, timedelta

import pytest

from app.strategies import REGISTRY
from app.strategies.pead_flagship import PeadFlagship
from app.strategies.pead_nosip import PeadNoSip
from app.services.signals.events import Event


class TestContract:
    def test_registered_alongside_the_flagship(self):
        assert REGISTRY["pead_nosip"] is PeadNoSip
        assert REGISTRY["pead_flagship"] is PeadFlagship

    def test_drops_sip_and_only_sip(self):
        assert PeadNoSip.requires == frozenset({"shorts", "llm"})
        assert PeadFlagship.requires == frozenset({"sip", "shorts", "llm"})

    def test_policy_params_are_the_flagship_s(self):
        """The variant must stay comparable to the paper: same Table 1
        values, two new plumbing knobs, nothing else changed."""
        base, mine = PeadFlagship.default_params, PeadNoSip.default_params
        assert {k: v for k, v in mine.items()
                if k not in ("quote_max_age_s", "verify_min")} == base
        assert mine["quote_max_age_s"] == 120.0
        assert mine["verify_min"] == 16.0

    def test_verify_floor_is_the_sip_delay(self):
        with pytest.raises(ValueError, match="15 minutes behind"):
            PeadNoSip.validate_params({"verify_min": 10.0})
        assert PeadNoSip.validate_params({"verify_min": 20.0})["verify_min"] == 20.0

    def test_quote_age_bounds(self):
        with pytest.raises(ValueError, match="quote_max_age_s"):
            PeadNoSip.validate_params({"quote_max_age_s": 5.0})


class _Ctx:
    """Just enough StrategyContext for the seam and the verifier."""

    def __init__(self, params, sip=None, sip_raises=False):
        self.params = params
        self.log = logging.getLogger("test.nosip")
        self.notes: list[tuple[dict, tuple]] = []
        self.ah_calls: list[tuple[str, float]] = []
        self._sip = sip
        self._sip_raises = sip_raises
        outer = self

        class _Market:
            async def ah_quote(self, sym, max_age_s=120.0):
                outer.ah_calls.append((sym, max_age_s))
                return {"bid": 50.0, "ask": 50.0, "mid": 50.0,
                        "ts": _time.time() * 1000, "src": "yahoo"}

            async def sip_quote_asof(self, sym, at_utc):
                if outer._sip_raises:
                    raise RuntimeError("subscription does not permit")
                return outer._sip

        self.market = _Market()

    async def note(self, detail, signal_ids=()):
        self.notes.append((detail, tuple(signal_ids)))


def _timer(hhmm="16:31") -> Event:
    # A Tuesday, outside the 15:30/16:04 special ticks.
    et = f"2026-08-11T{hhmm}:00-04:00"
    return Event(type="timer", ts=datetime.now(timezone.utc), source="timer",
                 key=None, symbols=(), payload={"kind": "tick", "et": et})


def _strategy_with_decision(side: int, price: float, params: dict,
                            src="yahoo") -> PeadNoSip:
    s = PeadNoSip()
    s._watch_date = "2026-08-11"
    quote = {"bid": price, "ask": price, "mid": price,
             "ts": _time.time() * 1000, "src": src}
    s._intent("ABCD", side, price, quote, 5000.0, 1, params, (7,))
    return s


class TestPricingSeam:
    @pytest.mark.asyncio
    async def test_quote_goes_through_ah_path_with_configured_age(self):
        params = PeadNoSip.validate_params({"quote_max_age_s": 90.0})
        ctx = _Ctx(params)
        s = PeadNoSip()
        q = await s._quote("ABCD", ctx)
        assert ctx.ah_calls == [("ABCD", 90.0)]
        assert q["src"] == "yahoo"


class TestFillCheck:
    @pytest.mark.asyncio
    async def test_long_marketability_and_cost_vs_true_mid(self):
        params = PeadNoSip.validate_params({})
        # True book at the entry instant: 49.80 / 50.00. A long limit at the
        # 50.00 print lifts the ask exactly -> marketable, ~+20bp above mid.
        ctx = _Ctx(params, sip={"bid": 49.80, "ask": 50.00,
                                "ts": 0, "src": "sip-delayed"})
        s = _strategy_with_decision(+1, 50.0, params)
        s._fill_checks["ABCD:2026-08-11"]["due_mono"] = _time.monotonic() - 1
        await s.on_event(_timer(), ctx)
        checks = [d["fill_check"] for d, _ in ctx.notes if "fill_check" in d]
        assert len(checks) == 1
        fc = checks[0]
        assert fc["marketable_at_limit"] is True
        assert fc["quote_src"] == "yahoo"
        assert fc["mode"] == "note"
        assert fc["print_vs_mid_bp"] == pytest.approx(20.0, abs=0.5)
        assert not s._fill_checks, "one-shot: the check retires itself"

    @pytest.mark.asyncio
    async def test_short_needs_the_bid_at_or_over_the_limit(self):
        params = PeadNoSip.validate_params({})
        ctx = _Ctx(params, sip={"bid": 49.50, "ask": 49.70,
                                "ts": 0, "src": "sip-delayed"})
        s = _strategy_with_decision(-1, 50.0, params)
        s._fill_checks["ABCD:2026-08-11"]["due_mono"] = _time.monotonic() - 1
        await s.on_event(_timer(), ctx)
        fc = next(d["fill_check"] for d, _ in ctx.notes if "fill_check" in d)
        # Short limit at 50.00 against a 49.50 bid: nobody pays up 50 -> rests.
        assert fc["marketable_at_limit"] is False

    @pytest.mark.asyncio
    async def test_not_due_yet_stays_queued(self):
        params = PeadNoSip.validate_params({})
        ctx = _Ctx(params, sip={"bid": 49.8, "ask": 50.0, "ts": 0})
        s = _strategy_with_decision(+1, 50.0, params)
        await s.on_event(_timer(), ctx)
        assert s._fill_checks, "verify_min has not elapsed"
        assert not [d for d, _ in ctx.notes if "fill_check" in d]

    @pytest.mark.asyncio
    async def test_sip_unavailable_is_reported_not_assumed(self):
        params = PeadNoSip.validate_params({})
        ctx = _Ctx(params, sip=None)
        s = _strategy_with_decision(+1, 50.0, params)
        s._fill_checks["ABCD:2026-08-11"]["due_mono"] = _time.monotonic() - 1
        await s.on_event(_timer(), ctx)
        skipped = [d for d, _ in ctx.notes
                   if "fill_check" in d and "why" in d]
        assert len(skipped) == 1
        assert "skipped, not assumed" in skipped[0]["why"]

    @pytest.mark.asyncio
    async def test_lookup_errors_do_not_kill_the_strategy(self):
        params = PeadNoSip.validate_params({})
        ctx = _Ctx(params, sip_raises=True)
        s = _strategy_with_decision(+1, 50.0, params)
        s._fill_checks["ABCD:2026-08-11"]["due_mono"] = _time.monotonic() - 1
        await s.on_event(_timer(), ctx)   # must not raise
        assert not s._fill_checks


class TestExitTimeUnchanged:
    def test_verification_never_outlives_the_free_tier_window(self):
        """verify_min's floor is the SIP delay; the entry_utc it checks is
        wall-clock, so by the time a check fires the queried instant is
        always inside the queryable past."""
        params = PeadNoSip.validate_params({})
        s = _strategy_with_decision(+1, 50.0, params)
        check = s._fill_checks["ABCD:2026-08-11"]
        due_in_s = check["due_mono"] - _time.monotonic()
        assert due_in_s >= 15 * 60
        assert check["entry_utc"] <= datetime.now(timezone.utc)
        assert check["entry_utc"] > datetime.now(timezone.utc) - timedelta(minutes=1)
