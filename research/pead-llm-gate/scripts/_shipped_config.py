"""The configuration the engine ran while the study was written.

Table 2 of the paper documents `earnings_reaction` — the 5% stop / 2x target
configuration the study reports as the WEAKEST thing it measured (38.3bp per
trade, 5.5% a year). The paper's own words for it: "one candidate among the
several this paper tests, not a reference implementation".

That class was read live out of `app/` so the paper could not drift from the
engine. On 2026-08-07 the engine stopped running it — `pead_flagship` replaced
it with the policy the paper actually concludes for — and a live read became
the opposite of what it was for: it would have shown Table 2 silently
following a strategy the table is not about.

So the values are frozen here, verbatim, as of the run that produced the
paper. This is a historical record now, and it should never be "updated" to
track anything. If the paper is ever re-run against a different production
configuration, the honest move is a new table, not an edit to this one.

Provenance: app/strategies/earnings_reaction.py at commit 1cd7e00.
"""

from datetime import time as dtime

WATCHLIST_ET = "15:30"
EXIT_ET = dtime(15, 55)

DEFAULT_PARAMS = {
    "n_names": 8,
    "min_price": 5.0,
    "risk_pct_per_name": 0.5,
    "min_move_pct": 5.0,
    "short_run5d_floor_pct": -5.0,
    "max_spread_pct": 2.0,
    "tp_pct": 0.10,
    "sl_pct": 0.05,
    "sl_pct_cap": 0.12,
    "vol_stop_mult": 2.5,
    "confirm_min": 10.0,
    "hold": "T+1",
    "live": False,
    "max_text": 12_000,
    "effort": "medium",
    "analysis_timeout_s": 90.0,
}
