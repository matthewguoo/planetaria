"""The registered block contract and the ladder math (services/registered).

The block is FROZEN data: these tests pin its schema so a drive-by edit
that breaks the console or the ladder aggregation fails loudly, and they
check the metric arithmetic on constructed plans — no DB, no broker.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.models.trade import TradePlan
from app.services.registered import ladder_state
from app.strategies import REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWN_METRICS = {"net_bp_per_trade", "bp_of_underlying_per_day",
                 "drift_hit_rate_nonneutral"}

DAY1 = datetime(2026, 8, 11, 13, 35, tzinfo=timezone.utc)   # 09:35 ET
DAY2 = datetime(2026, 8, 12, 13, 35, tzinfo=timezone.utc)


def test_registered_blocks_are_valid():
    seen = 0
    for cls in REGISTRY.values():
        block = cls.registered
        if block is None:
            continue
        seen += 1
        json.dumps(block)  # API-serializable or bust
        assert block["metric"] in KNOWN_METRICS
        lo, hi = block["band"]
        assert lo < hi
        assert (REPO_ROOT / block["doc"]).is_file(), block["doc"]
        assert block["registered_commit"]
        stages = [s["stage"] for s in block["ladder"]]
        assert len(stages) >= 2 and len(set(stages)) == len(stages)
    assert seen >= 3  # afternoon_fly, gap_fail_fade, pead_nosip


def test_unmeasured_metric_never_falls_back():
    """nosip registers drift hit rate; the engine doesn't compute it yet.
    The ladder must say so — NOT quietly measure per-trade bp instead."""
    block = REGISTRY["pead_nosip"].registered
    plans = [_equity_plan(DAY1, 100.0, 1, 0.11)]  # would be +11bp if measured
    state = ladder_state(block, {"live": True}, plans, [])
    assert state["metric_computed"] is False
    assert state["running_metric"] is None and state["band_status"] is None
    assert state["trades_closed"] == 1
    assert state["stage"] == "live forward test"


def _equity_plan(created: datetime, entry: float, qty: int,
                 realized: float | None) -> TradePlan:
    return TradePlan(
        underlying="GAPD", strategy="gff-1", asset_class="equity",
        legs=[{"symbol": "GAPD", "side": 1, "ratio": 1, "entry": entry}],
        qty=qty, entry_limit=entry, fill_premium=entry,
        tp_premium=None, sl_premium=None, time_stop_utc=created,
        created_at=created, status="sim_closed", realized_pnl=realized,
    )


def _fly_plan(created: datetime, strike: float, qty: int,
              realized: float) -> TradePlan:
    legs = [
        {"symbol": "S1", "right": "C", "strike": strike, "side": -1, "ratio": 1},
        {"symbol": "S2", "right": "P", "strike": strike, "side": -1, "ratio": 1},
        {"symbol": "S3", "right": "C", "strike": strike * 1.01, "side": 1, "ratio": 1},
        {"symbol": "S4", "right": "P", "strike": strike * 0.99, "side": 1, "ratio": 1},
    ]
    return TradePlan(
        underlying="QQQ", strategy="fly-1", legs=legs, qty=qty,
        entry_limit=-2.8, fill_premium=-2.8, tp_premium=None,
        sl_premium=None, time_stop_utc=created, created_at=created,
        status="sim_closed", realized_pnl=realized,
    )


def test_unregistered_kind_has_no_ladder():
    assert ladder_state(None, {"live": False}, [], []) is None


def test_net_bp_per_trade_running_mean_and_band():
    block = REGISTRY["gap_fail_fade"].registered
    # one share at $100, +11 cents realized = +11.0bp — inside [8, 15]
    plans = [_equity_plan(DAY1, 100.0, 1, 0.11)]
    state = ladder_state(block, {"live": False}, plans, [])
    assert state["running_metric"] == 11.0
    assert state["band_status"] == "in"
    assert state["stage"] == "note-mode"
    assert state["target_sessions"] == 20
    # a second, huge trade drags the running mean above the band
    plans.append(_equity_plan(DAY2, 100.0, 1, 4.0))  # +400bp
    state = ladder_state(block, {"live": False}, plans, [])
    assert state["running_metric"] == 205.5
    assert state["band_status"] == "above"
    assert state["trades_closed"] == 2 and state["samples"] == 2


def test_open_plans_count_toward_sessions_not_metric():
    block = REGISTRY["gap_fail_fade"].registered
    plans = [_equity_plan(DAY1, 100.0, 1, None)]      # open: no realized
    naive_ts = datetime(2026, 8, 12, 13, 20)          # SQLite-style naive UTC
    state = ladder_state(block, {"live": True}, plans, [naive_ts])
    assert state["sessions_observed"] == 2            # DAY1 plan + DAY2 note
    assert state["trades_closed"] == 0
    assert state["running_metric"] is None and state["band_status"] is None
    assert state["stage"] == "one-share live" and state["target_sessions"] == 10


def test_fly_metric_is_per_day_bp_of_underlying():
    block = REGISTRY["afternoon_fly"].registered
    # day 1: two 1-set flies at K=560 -> denom 2 x 56,000; +33.6 = +3.0bp
    plans = [_fly_plan(DAY1, 560.0, 1, 16.8), _fly_plan(DAY1, 560.0, 1, 16.8)]
    state = ladder_state(block, {"live": False}, plans, [])
    assert state["samples"] == 1                      # one traded DAY
    assert state["running_metric"] == 3.0
    assert state["band_status"] == "above"            # band is [1.5, 2.5]
    # day 2 loses -56.0 on one set (-10bp): mean (3.0 - 10.0)/2 = -3.5
    plans.append(_fly_plan(DAY2, 560.0, 1, -56.0))
    state = ladder_state(block, {"live": False}, plans, [])
    assert state["samples"] == 2
    assert state["running_metric"] == -3.5
    assert state["band_status"] == "below"
