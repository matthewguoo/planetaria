"""Paper twin math. Pure functions, no engine — the whole point of
services/sim_account is that the two allocator curves are reproducible from
journal rows alone."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.sim_account import (
    COST_ENTRY_BP,
    COST_EXIT_BP,
    signals_from_decisions,
    simulate,
)

T0 = datetime(2026, 8, 5, 20, 30, tzinfo=timezone.utc)


def bars(prices, start=T0, step_s=60):
    """1m bars whose high/low straddle each close by 0.1%."""
    out = []
    for i, px in enumerate(prices):
        ts = start + timedelta(seconds=step_s * i)
        out.append({"t": ts.timestamp() * 1000, "o": px, "h": px * 1.001,
                    "l": px * 0.999, "c": px, "v": 1000})
    return out


def decision(symbol="AAA", side=1, px=100.0, sl=0.05, conf="medium",
             flags=None, ts=T0, row_id=1, key="would_trade", time_stop=None,
             run5d=None):
    return {
        "id": row_id, "ts": ts.isoformat(), "strategy_id": "s1",
        "action": "note",
        "detail": {key: {
            "symbol": symbol, "side": side, "px": px, "move_pct": 6.0,
            "run5d_pct": run5d,
            "verdict": {"direction": "bullish" if side > 0 else "bearish",
                        "confidence": conf, "quality_flags": flags or []},
            "bracket": {"sl_pct": sl, "tp_pct": 2 * sl},
            "intent": {"time_stop_utc": (time_stop or (T0 + timedelta(hours=20))).isoformat()},
        }},
    }


class TestSignalExtraction:
    def test_both_modes_produce_signals(self):
        rows = [decision(row_id=1, key="would_trade"),
                decision(row_id=2, key="decision", symbol="BBB")]
        signals = signals_from_decisions(rows)
        assert [s["symbol"] for s in signals] == ["AAA", "BBB"]

    def test_non_trades_are_ignored(self):
        rows = [
            {"id": 1, "ts": T0.isoformat(), "action": "note",
             "detail": {"no_trade": {"symbol": "AAA"}}},          # no side
            {"id": 2, "ts": T0.isoformat(), "action": "note",
             "detail": {"watchlist": {"AAA": {}}}},
            {"id": 3, "ts": T0.isoformat(), "action": "note", "detail": {}},
        ]
        assert signals_from_decisions(rows) == []

    def test_ordered_oldest_first(self):
        rows = [decision(row_id=9, symbol="ZZZ"), decision(row_id=2, symbol="AAA")]
        assert [s["symbol"] for s in signals_from_decisions(rows)] == ["AAA", "ZZZ"]


class TestAllocators:
    """Same entry, same bracket, same exit — only size differs."""

    def _run(self, policy, **kw):
        signals = signals_from_decisions([decision(**kw)])
        return simulate(signals, lambda s: [], lambda s: None, policy=policy,
                        start_equity=100_000.0)

    def test_vol_sizes_by_stop_width(self):
        tight = self._run("vol", sl=0.05)["positions"][0]
        wide = self._run("vol", sl=0.10)["positions"][0]
        # Twice the stop, half the shares: identical dollars at the stop.
        assert wide["qty"] == tight["qty"] // 2
        assert tight["qty"] * 100.0 * 0.05 == 500.0   # 0.5% of 100k

    def test_flat_ignores_stop_width(self):
        tight = self._run("flat", sl=0.05)["positions"][0]
        wide = self._run("flat", sl=0.10)["positions"][0]
        assert wide["qty"] == tight["qty"] == 100   # 10% of 100k at $100

    def test_vol_scales_with_conviction_flat_does_not(self):
        hi = self._run("vol", conf="high")["positions"][0]["qty"]
        lo = self._run("vol", conf="low")["positions"][0]["qty"]
        assert hi == 3 * lo
        assert (self._run("flat", conf="high")["positions"][0]["qty"]
                == self._run("flat", conf="low")["positions"][0]["qty"])

    def test_quality_flags_shrink_vol_size(self):
        clean = self._run("vol")["positions"][0]["qty"]
        flagged = self._run("vol", flags=["margin_pressure"])["positions"][0]["qty"]
        assert flagged == int(clean * 0.75)


class TestPathResolution:
    def test_take_profit(self):
        signals = signals_from_decisions([decision(sl=0.05)])
        out = simulate(signals, lambda s: bars([100, 105, 111]), lambda s: None,
                       policy="flat", start_equity=100_000.0)
        trade = out["positions"][0]
        assert trade["exit_reason"] == "tp"
        assert trade["exit"] == pytest.approx(110.0)
        assert out["realized"] > 0

    def test_stop_loss(self):
        signals = signals_from_decisions([decision(sl=0.05)])
        out = simulate(signals, lambda s: bars([100, 97, 94]), lambda s: None,
                       policy="flat", start_equity=100_000.0)
        assert out["positions"][0]["exit_reason"] == "sl"
        assert out["realized"] < 0

    def test_same_bar_touch_scores_the_loss(self):
        """A bar that trades through both brackets is a stop, never a win —
        daily/minute data cannot order intrabar touches."""
        both = [{"t": (T0 + timedelta(minutes=1)).timestamp() * 1000,
                 "o": 100, "h": 120, "l": 90, "c": 100, "v": 1}]
        signals = signals_from_decisions([decision(sl=0.05)])
        out = simulate(signals, lambda s: both, lambda s: None, policy="flat",
                       start_equity=100_000.0)
        assert out["positions"][0]["exit_reason"] == "sl"

    def test_time_stop_closes_at_the_close(self):
        stop = T0 + timedelta(minutes=2)
        signals = signals_from_decisions([decision(sl=0.20, time_stop=stop)])
        out = simulate(signals, lambda s: bars([100, 101, 102, 103]),
                       lambda s: None, policy="flat", start_equity=100_000.0)
        trade = out["positions"][0]
        assert trade["exit_reason"] == "time_stop"
        assert trade["exit"] == 102

    def test_short_side_inverts(self):
        signals = signals_from_decisions([decision(side=-1, sl=0.05)])
        out = simulate(signals, lambda s: bars([100, 95, 89]), lambda s: None,
                       policy="flat", start_equity=100_000.0)
        trade = out["positions"][0]
        assert trade["exit_reason"] == "tp"
        assert out["realized"] > 0

    def test_unresolved_position_stays_open_and_marked(self):
        signals = signals_from_decisions([decision(sl=0.20)])
        out = simulate(signals, lambda s: [], lambda s: 104.0, policy="flat",
                       start_equity=100_000.0)
        trade = out["positions"][0]
        assert trade["exit"] is None and trade["exit_reason"] is None
        assert trade["mark"] == 104.0
        assert out["realized"] == 0 and out["unrealized"] > 0
        # Marked equity moves; realized does not.
        assert out["equity"] > out["start_equity"]

    def test_no_bars_and_no_quote_is_not_a_fabricated_price(self):
        signals = signals_from_decisions([decision()])
        out = simulate(signals, lambda s: [], lambda s: None, policy="flat",
                       start_equity=100_000.0)
        trade = out["positions"][0]
        assert trade["pnl"] is None and trade["unrealized"] is None
        assert out["equity"] == out["start_equity"]


class TestLedger:
    def test_costs_are_charged_once_per_round_trip(self):
        signals = signals_from_decisions([decision(sl=0.05)])
        out = simulate(signals, lambda s: bars([100, 100.0]), lambda s: None,
                       policy="flat", start_equity=100_000.0)
        trade = out["positions"][0]
        expected = -trade["notional"] * (COST_ENTRY_BP + COST_EXIT_BP) / 1e4
        assert trade["unrealized"] == round(expected, 2)

    def test_equity_compounds_across_trades(self):
        later = T0 + timedelta(minutes=10)
        rows = [decision(row_id=1, symbol="AAA", sl=0.5),
                decision(row_id=2, symbol="BBB", sl=0.5, ts=later)]
        signals = signals_from_decisions(rows)
        panel = {"AAA": bars([100, 201]), "BBB": bars([100, 201], start=later)}
        out = simulate(signals, panel.get, lambda s: None, policy="flat",
                       start_equity=100_000.0)
        # Second entry is sized off the equity the first one produced, so it
        # buys more shares at the same price.
        first, second = out["positions"][1], out["positions"][0]
        assert first["symbol"] == "AAA" and second["symbol"] == "BBB"
        assert second["qty"] > first["qty"]
        assert out["closed"] == 2 and out["win_rate"] == 1.0

    def test_curve_starts_at_start_equity_and_ends_marked(self):
        signals = signals_from_decisions([decision(sl=0.05)])
        out = simulate(signals, lambda s: bars([100, 111]), lambda s: None,
                       policy="flat", start_equity=50_000.0)
        assert out["curve"][0]["equity"] == 50_000.0
        assert out["curve"][-1]["equity"] == out["equity"]
        assert out["max_drawdown"] == 0.0
