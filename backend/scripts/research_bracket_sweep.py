"""What bracket should the earnings-reaction strategy actually use?

The `brackets` stage in research_llm_contamination.py answers one question —
what did the SHIPPED bracket cost — by refetching minute bars and walking a
single configuration. That is the wrong shape for CHOOSING a bracket: every
candidate would mean another full refetch.

So cache the PATH once and sweep offline. For each gated event this stores,
per excursion level, the first minute at which the price reached that level
in each direction. That is all a bracket needs: for any (stop, target) pair,
whichever level is touched first decides the trade, and ties go to the stop.
One API pass, then any configuration resolves instantly.

IN-SAMPLE OPTIMISATION IS THE HAZARD, so the sweep never reports a single
winner. It reports the surface and splits the sample by time — 2021-23
against 2024-26. A configuration that only wins on the half it was chosen
from is a curve fit, and the split is what makes that visible instead of
flattering.

Run: .venv/Scripts/python.exe scripts/research_bracket_sweep.py paths
     .venv/Scripts/python.exe scripts/research_bracket_sweep.py sweep
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_llm_contamination import (  # noqa: E402
    CACHE,
    COSTS_BP,
    ET,
    SL_CAP,
    SL_FLOOR,
    SL_MULT,
    _avg_abs_ret,
    _gate,
    _load_avg_cache,
    _load_results,
    load_universe,
)

PATHS = CACHE / "event_paths.parquet"
DOC = Path(__file__).resolve().parents[2] / "docs" / "notes"

# Excursion grid, percent. Storing first-touch time per level makes any
# (stop, target) resolvable without keeping the raw bars.
LEVELS = np.round(np.arange(0.5, 30.01, 0.5), 2)


def gated_events(effort: str) -> pd.DataFrame:
    universe = load_universe().rename(columns={"date": "edate"})
    universe["date"] = universe["edate"].astype(str)
    res = _load_results()
    res = res[(res["effort"] == effort) & (res["arm"] == "named")]
    m = res.merge(universe, on=["symbol", "date"], how="inner")
    m = m[_gate(m)].copy()
    m["side"] = np.sign(m["move_pct"]).astype(int)
    return m


def stage_paths(args) -> None:
    """One API pass: for each gated event, how many minutes until the price
    first reaches each excursion level, each way, plus the T+1 close.
    -1 means the level was never reached."""
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    m = gated_events(args.effort)
    have: set = set()
    prior = pd.DataFrame()
    if PATHS.exists():
        prior = pd.read_parquet(PATHS)
        have = set(zip(prior["symbol"], prior["date"]))
    todo = [r for _, r in m.iterrows() if (r["symbol"], r["date"]) not in have]
    print(f"{len(todo)} of {len(m)} gated events need paths")

    settings = get_settings()
    api = StockHistoricalDataClient(settings.alpaca_api_key,
                                    settings.alpaca_secret_key)
    rows = []
    for n, row in enumerate(todo):
        d = row["edate"]
        entry = float(row["react"])
        start = datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET)
        try:
            out = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=row["symbol"], timeframe=TimeFrame.Minute,
                start=start, end=start + pd.Timedelta(days=4).to_pytimedelta(),
                feed="sip"))
            bars = out.data.get(row["symbol"]) or []
        except Exception as exc:
            print(f"  {row['symbol']} {d}: {str(exc)[:60]}")
            bars = []
        up = np.full(len(LEVELS), -1.0)
        dn = np.full(len(LEVELS), -1.0)
        last_close, minute = entry, 0
        for bar in bars:
            ts = bar.timestamp.astimezone(ET)
            if ts.date() == d and ts.hour * 60 + ts.minute < 16 * 60 + 20:
                continue
            if ts.date() > d and (ts.hour * 60 + ts.minute) > 15 * 60 + 55:
                break
            minute += 1
            hi_pct = (float(bar.high) / entry - 1) * 100
            lo_pct = (float(bar.low) / entry - 1) * 100
            last_close = float(bar.close)
            np.putmask(up, (up < 0) & (LEVELS <= hi_pct), minute)
            np.putmask(dn, (dn < 0) & (LEVELS <= -lo_pct), minute)
        rows.append({"symbol": row["symbol"], "date": row["date"],
                     "side": int(row["side"]), "entry": entry,
                     "close": last_close, "bars": minute,
                     "up": up.tolist(), "dn": dn.tolist()})
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}")
        _time.sleep(0.22)
    out_df = (pd.concat([prior, pd.DataFrame(rows)], ignore_index=True)
              if len(prior) else pd.DataFrame(rows))
    out_df.to_parquet(PATHS)
    print(f"cached {len(out_df)} paths -> {PATHS}")


def _resolve(paths: pd.DataFrame, stop_pct: np.ndarray,
             target_pct: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per event, which bracket level is touched first.

    A long stops on a DOWN excursion and targets an UP one; a short is the
    mirror. Ties resolve to the stop — a bar that trades through both levels
    cannot be ordered from minute data, so it is scored as the loss."""
    up = np.stack(paths["up"].to_numpy())
    dn = np.stack(paths["dn"].to_numpy())
    side = paths["side"].to_numpy()
    entry = paths["entry"].to_numpy()
    close = paths["close"].to_numpy()

    def first_touch(levels_pct, matrix):
        idx = np.clip(np.searchsorted(LEVELS, levels_pct - 1e-9),
                      0, len(LEVELS) - 1)
        beyond = levels_pct > LEVELS[-1]
        t = matrix[np.arange(len(idx)), idx]
        t = np.where(t < 0, np.inf, t)
        return np.where(beyond, np.inf, t)

    is_long = side > 0
    t_stop = np.where(is_long, first_touch(stop_pct, dn), first_touch(stop_pct, up))
    t_tgt = np.where(is_long, first_touch(target_pct, up), first_touch(target_pct, dn))
    stopped = np.isfinite(t_stop) & (t_stop <= t_tgt)
    targeted = np.isfinite(t_tgt) & (t_tgt < t_stop)
    ret = np.where(stopped, -stop_pct / 100,
                   np.where(targeted, target_pct / 100,
                            side * (close / entry - 1)))
    reason = np.where(stopped, "sl", np.where(targeted, "tp", "time"))
    return ret - COSTS_BP / 1e4, reason


def stage_sweep(args) -> None:
    if not PATHS.exists():
        raise SystemExit("run the `paths` stage first")
    paths = pd.read_parquet(PATHS)
    cols = ["symbol", "date", "edate", "year"]
    paths = paths.merge(gated_events(args.effort)[cols], on=["symbol", "date"],
                        how="inner")
    _load_avg_cache()
    avg = np.array([(_avg_abs_ret(r["symbol"], r["edate"]) or SL_FLOOR / SL_MULT)
                    for _, r in paths.iterrows()])
    vol_stop = np.clip(SL_MULT * avg, SL_FLOOR, SL_CAP) * 100
    n = len(paths)
    early = paths["year"].isin(["2021", "2022", "2023"]).to_numpy()
    late = ~early
    print(f"\n{n} gated events with cached paths "
          f"({int(early.sum())} in 2021-23, {int(late.sum())} in 2024-26)\n")

    def score(stop, target, label_stop, label_tgt):
        ret, reason = _resolve(paths, stop, target)
        return {"stop": label_stop, "target": label_tgt,
                "all": ret.mean() * 1e4, "early": ret[early].mean() * 1e4,
                "late": ret[late].mean() * 1e4,
                "win": float((ret > 0).mean()) * 100,
                "stopped": float((reason == "sl").mean()) * 100}

    NONE = np.full(n, 1e9)
    grid = []
    for sp in (3, 4, 5, 6, 8, 10, 12, 15, 20):
        stop = np.full(n, float(sp))
        grid.append(score(stop, NONE, f"{sp}%", "none"))
        for tr in (1.0, 1.5, 2.0, 3.0):
            grid.append(score(stop, stop * tr, f"{sp}%", f"{tr:g}x"))
    for tgt in (5, 8, 10, 15, 20):
        grid.append(score(NONE, np.full(n, float(tgt)), "none", f"{tgt}%"))
    grid.append(score(NONE, NONE, "none", "none"))
    shipped = score(vol_stop, vol_stop * 2, "vol x2.5", "2x")

    df = pd.DataFrame(grid)
    hdr = (f"{'stop':>10}{'target':>9}{'all':>9}{'2021-23':>10}"
           f"{'2024-26':>10}{'win%':>8}{'stopped%':>10}")
    print(hdr)
    print("-" * len(hdr))
    for _, r in df.iterrows():
        print(f"{r['stop']:>10}{r['target']:>9}{r['all']:>+9.1f}"
              f"{r['early']:>+10.1f}{r['late']:>+10.1f}"
              f"{r['win']:>8.1f}{r['stopped']:>10.1f}")
    print("-" * len(hdr))
    r = shipped
    print(f"{r['stop']:>10}{r['target']:>9}{r['all']:>+9.1f}"
          f"{r['early']:>+10.1f}{r['late']:>+10.1f}"
          f"{r['win']:>8.1f}{r['stopped']:>10.1f}   <- SHIPPED")

    best = df.loc[df["early"].idxmax()]
    print(f"\nchosen on 2021-23 alone: stop {best['stop']} target {best['target']}"
          f" -> {best['early']:+.1f}bp in sample, {best['late']:+.1f}bp OUT of "
          f"sample (shipped: {shipped['late']:+.1f}bp out of sample)")
    print("Read the two period columns together. A row that wins only on "
          "2021-23 is a curve fit.")

    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    DOC.mkdir(parents=True, exist_ok=True)
    lines = [f"# Bracket sweep — {stamp}", "",
             f"{n} gated events; per-event minute paths cached once and swept "
             f"offline. Costs {COSTS_BP:.0f}bp round trip; same-bar ties "
             f"resolve to the stop. 2021-23 vs 2024-26 split is the guard "
             f"against fitting the surface.", "",
             "| stop | target | all | 2021-23 | 2024-26 | win% | stopped% |",
             "|---|---|---|---|---|---|---|"]
    for _, row in pd.concat([df, pd.DataFrame([shipped])]).iterrows():
        lines.append(f"| {row['stop']} | {row['target']} | {row['all']:+.1f} | "
                     f"{row['early']:+.1f} | {row['late']:+.1f} | "
                     f"{row['win']:.1f} | {row['stopped']:.1f} |")
    path = DOC / f"bracket_sweep_{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    df.to_parquet(CACHE / "bracket_sweep.parquet")
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["paths", "sweep"])
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()
    {"paths": stage_paths, "sweep": stage_sweep}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
