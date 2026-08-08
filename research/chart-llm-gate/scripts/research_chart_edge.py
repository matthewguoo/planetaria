"""Is there anything to fix? Predictive content, stripped of the bracket.

THE QUESTION THIS ANSWERS. The mechanical run says all five setups lose money
and the bracket sweep says no stop/target combination rescues them. Those two
facts are consistent with two VERY different worlds, and everything about what
to do next depends on which one this is:

  world A   The patterns carry no forward information. Signed return is ~0 at
            every horizon, the strategies are noise, and the loss is simply
            the cost of churning. Nothing about exits, sizing or filtering
            fixes this, because there is no signal being destroyed — there is
            no signal.

  world B   The patterns DO predict, but the 2R bracket converts a real edge
            into a loss: a stop placed at structure gets tagged by noise
            before the drift arrives, so the strategy is right about direction
            and wrong about survival. This is very fixable, and the fix is in
            the exit, not the entry.

A bracketed backtest cannot tell these apart, because the stop censors the
outcome. Measuring the RAW signed forward return at a grid of horizons can:
it is the pattern's information content with the exit removed.

    signed return = side x (close[i+h] / close[i] - 1)

reported in bp, gross, with a t-stat. Held out of the session, so a horizon
that would run past 16:00 is truncated at the close rather than gapped.

The comparison that makes it readable is the SAME statistic on random entries
in the same tape, which is not zero — a random long in a year with a positive
drift makes money. What matters is the difference.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from _paths import BACKEND, NOTES

sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_chart_data import COST_BP, bars_path  # noqa: E402
from research_chart_setups import SETUPS, STRATEGIES, TF, prep  # noqa: E402

HORIZONS = [3, 6, 12, 24, 48, 78]        # bars; 78 x 5m = one full session


def _signed_forward(df: pd.DataFrame, idx: np.ndarray, side: np.ndarray,
                    h: int) -> np.ndarray:
    """Signed return h bars ahead, truncated at the session close.

    Truncation rather than exclusion: dropping the trades that run into the
    close would drop every afternoon setup and silently make this a
    morning-only statistic.
    """
    c = df["c"].to_numpy()
    day = df["day"].to_numpy()
    n = len(c)
    j = np.minimum(idx + h, n - 1)
    # Walk any index that crossed into the next session back to that session's
    # last bar.
    bad = day[j] != day[idx]
    while bad.any():
        j[bad] -= 1
        bad = day[j] != day[idx]
    return side * (c[j] / c[idx] - 1) * 1e4


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-null", type=int, default=4000)
    args = ap.parse_args()

    s = pd.read_parquet(SETUPS)
    rng = np.random.default_rng(20260807)
    frames = {sym: prep(pd.read_parquet(bars_path(sym)), TF)
              for sym in s["symbol"].unique()}

    lines: list[str] = []

    def emit(t: str = "") -> None:
        print(t)
        lines.append(t)

    emit(f"# Predictive content with the bracket removed — {date.today()}")
    emit("")
    emit(f"{len(s):,} setups. Mean SIGNED forward return in bp — "
         f"`side x (close[i+h]/close[i] - 1)` — gross of costs, truncated at "
         f"the session close. No stop, no target: this is what the pattern "
         f"knows, before any exit rule gets to destroy or preserve it.")
    emit("")
    emit("| strategy | n | " + " | ".join(f"{h}b" for h in HORIZONS) + " |")
    emit("|---" * (len(HORIZONS) + 2) + "|")

    per_strategy = {}
    for name in [*STRATEGIES, "ALL"]:
        g = s if name == "ALL" else s[s["strategy"] == name]
        if g.empty:
            continue
        cells, means = [], {}
        for h in HORIZONS:
            vals = []
            for sym, gs in g.groupby("symbol"):
                df = frames[sym]
                vals.append(_signed_forward(df, gs["i"].to_numpy(),
                                            gs["side"].to_numpy(), h))
            v = np.concatenate(vals)
            t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
            cells.append(f"{v.mean():+.1f} ({t:+.1f})")
            means[h] = v.mean()
        per_strategy[name] = means
        emit(f"| {name} | {len(g):,} | " + " | ".join(cells) + " |")
    emit("")
    emit("Cells are `mean bp (t-stat)`.")
    emit("")

    # ---------------------------------------------------------------- null
    emit("## The same statistic on random entries")
    emit("")
    emit("Not zero, and that is the point: a random LONG in a year that "
         "drifted up makes money, so the strategies must be read against "
         "this row and not against zero. Random entries here take a random "
         "side at a random RTH bar in the same tape.")
    emit("")
    null = {}
    vals_by_h: dict[int, list] = {h: [] for h in HORIZONS}
    per_sym = max(1, args.n_null // len(frames))
    for sym, df in frames.items():
        n = len(df)
        # Start past bar 50 so a random entry has the same amount of history
        # behind it that a real setup does, and stop short of the longest
        # horizon so no draw runs off the end of the frame.
        idx = rng.integers(50, n - max(HORIZONS) - 1, size=per_sym)
        side = rng.choice([1, -1], size=per_sym)
        for h in HORIZONS:
            vals_by_h[h].append(_signed_forward(df, idx, side, h))
    cells = []
    for h in HORIZONS:
        v = np.concatenate(vals_by_h[h])
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
        null[h] = v.mean()
        cells.append(f"{v.mean():+.1f} ({t:+.1f})")
    emit("| arm | n | " + " | ".join(f"{h}b" for h in HORIZONS) + " |")
    emit("|---" * (len(HORIZONS) + 2) + "|")
    emit(f"| random | {per_sym * len(frames):,} | " + " | ".join(cells) + " |")
    emit("")

    # ------------------------------------------------------------- verdict
    emit("## Edge over random, per strategy (bp)")
    emit("")
    emit("| strategy | " + " | ".join(f"{h}b" for h in HORIZONS) + " |")
    emit("|---" * (len(HORIZONS) + 1) + "|")
    for name, means in per_strategy.items():
        emit(f"| {name} | " + " | ".join(f"{means[h] - null[h]:+.1f}"
                                         for h in HORIZONS) + " |")
    emit("")
    emit(f"A strategy needs to clear {COST_BP:.0f}bp of round-trip cost on "
         f"top of the random row before any exit rule can help it. If these "
         f"differences are ~0 at every horizon, the patterns carry no "
         f"information and no stop, target, filter or position-sizing scheme "
         f"recovers an edge that was never there — the only honest fixes are "
         f"a different signal or a cheaper way to trade. If they are "
         f"materially positive at some horizon while the bracketed backtest "
         f"loses, then the bracket is the problem and the exit is worth "
         f"real work.")

    NOTES.mkdir(parents=True, exist_ok=True)
    p = NOTES / f"edge_horizons_{date.today():%Y%m%d}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {p}")


if __name__ == "__main__":
    main()
