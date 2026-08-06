"""How long should an LLM-gated earnings-reaction trade be held?

Three jobs, in order.

1. CORRECT THE EXIT. Every panel cached before 2026-08-06 selected its exit
   with `bars[date > d]` and `iloc[-1]` over a four-calendar-day fetch, so it
   exited on the LAST session in the window — T+4 for a Monday reporter, T+2
   for a Friday one. Everything labelled "T+1 15:55 exit" is really a 2-4
   session hold. This module re-derives the exit per session from scratch.

2. SWEEP FIXED HORIZONS. T+1 through T+5, at the close, with and without the
   bracket configurations the sweep found. Post-earnings drift is documented
   over weeks (Bernard & Thomas 1989), so the one-session horizon the engine
   ships is a choice, not a given, and it should be measured.

3. TEST CONDITIONAL EXTENSION. The PEAD literature's central claim is that
   drift is monotone in the magnitude of the surprise and in how much of it
   the market has yet to absorb. That implies holding longer when the signal
   is strong and cutting early when it is not, rather than a fixed horizon
   for every trade. The rules tested here extend the hold on:
     - surprise magnitude (|reaction|),
     - the model's own confidence and its guidance read,
     - confirmation: whether T+1 closed in the trade's favour,
     - and the inverse of the pre-print run-up ("already priced in").

   Each rule is scored on 2021-23 and 2024-26 separately. A rule that only
   works on the half it was designed against is a curve fit.

Caching: ONE Alpaca pass stores, per event, the per-session closes and the
first-touch minute for every excursion level across the whole window. After
that every horizon, every bracket and every conditional rule resolves
offline. No LLM calls — the cached verdicts are reused untouched.

Run: .venv/Scripts/python.exe scripts/research_holding_period.py paths
     .venv/Scripts/python.exe scripts/research_holding_period.py sweep
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
    _gate,
    _load_results,
    load_universe,
)

PATHS = CACHE / "event_paths_multi.parquet"
DOC = Path(__file__).resolve().parents[2] / "docs" / "notes"

LEVELS = np.round(np.arange(0.5, 40.01, 0.5), 2)
MAX_SESSIONS = 5
FETCH_DAYS = 11          # calendar days: enough for 5 sessions over a holiday


def scored_events(effort: str) -> pd.DataFrame:
    """Every event the study scored — gated AND vetoed. The vetoed leg is
    needed to restate the gate spread on a corrected exit."""
    universe = load_universe().rename(columns={"date": "edate"})
    universe["date"] = universe["edate"].astype(str)
    res = _load_results()
    res = res[(res["effort"] == effort) & (res["arm"] == "named")]
    m = res.merge(universe, on=["symbol", "date"], how="inner")
    m["side"] = np.sign(m["move_pct"]).astype(int)
    m["gated"] = _gate(m)
    return m


def stage_paths(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    m = scored_events(args.effort)
    prior, have = pd.DataFrame(), set()
    if PATHS.exists():
        prior = pd.read_parquet(PATHS)
        have = set(zip(prior["symbol"], prior["date"]))
    todo = [r for _, r in m.iterrows() if (r["symbol"], r["date"]) not in have]
    print(f"{len(todo)} of {len(m)} scored events need multi-session paths")

    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows = []
    for n, row in enumerate(todo):
        d, entry = row["edate"], float(row["react"])
        start = datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET)
        try:
            out = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=row["symbol"], timeframe=TimeFrame.Minute,
                start=start,
                end=start + pd.Timedelta(days=FETCH_DAYS).to_pytimedelta(),
                feed="sip"))
            bars = out.data.get(row["symbol"]) or []
        except Exception as exc:
            print(f"  {row['symbol']} {d}: {str(exc)[:60]}")
            bars = []
        up = np.full(len(LEVELS), -1.0)
        dn = np.full(len(LEVELS), -1.0)
        closes, close_minutes = [], []
        minute, sess_date, sess_close = 0, None, None
        for bar in bars:
            ts = bar.timestamp.astimezone(ET)
            hm = ts.hour * 60 + ts.minute
            if ts.date() == d and hm < 16 * 60 + 20:
                continue                     # before the entry print
            if ts.date() > d:
                if sess_date is not None and ts.date() != sess_date and sess_close:
                    closes.append(sess_close)
                    close_minutes.append(minute)
                    if len(closes) >= MAX_SESSIONS:
                        break
                sess_date = ts.date()
                if hm <= 15 * 60 + 55:
                    sess_close = float(bar.close)   # last RTH print so far
            minute += 1
            np.putmask(up, (up < 0) & (LEVELS <= (float(bar.high) / entry - 1) * 100), minute)
            np.putmask(dn, (dn < 0) & (LEVELS <= -(float(bar.low) / entry - 1) * 100), minute)
        if sess_close and len(closes) < MAX_SESSIONS:
            closes.append(sess_close)
            close_minutes.append(minute)
        if not closes:
            continue
        rows.append({
            "symbol": row["symbol"], "date": row["date"], "side": int(row["side"]),
            "entry": entry, "gated": bool(row["gated"]),
            "closes": closes, "close_minutes": close_minutes,
            "up": up.tolist(), "dn": dn.tolist(),
        })
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}")
        _time.sleep(0.22)
    df = (pd.concat([prior, pd.DataFrame(rows)], ignore_index=True)
          if len(prior) else pd.DataFrame(rows))
    df.to_parquet(PATHS)
    print(f"cached {len(df)} multi-session paths -> {PATHS}")


# ------------------------------------------------------------------ resolve

def _pad(seq_col: pd.Series, k: int, fill) -> np.ndarray:
    """Column k of a ragged list column: some events have fewer sessions
    (holidays, halts, the end of the data). Missing sessions inherit the last
    one available rather than dropping the trade."""
    out = []
    for seq in seq_col:
        seq = list(seq)
        out.append(seq[min(k, len(seq) - 1)] if seq else fill)
    return np.asarray(out, dtype=float)


def resolve(paths: pd.DataFrame, horizon: np.ndarray,
            stop_pct: np.ndarray | None,
            target_pct: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """Return (net return, exit reason) for a per-event horizon in sessions,
    optionally bracketed. A bracket only counts if it is touched BEFORE the
    horizon's close — the whole point of a longer hold is that an excursion
    on day 2 is not an exit if you were always holding to day 3."""
    up = np.stack(paths["up"].to_numpy())
    dn = np.stack(paths["dn"].to_numpy())
    side = paths["side"].to_numpy()
    entry = paths["entry"].to_numpy()
    n = len(paths)

    close_px = np.empty(n)
    deadline = np.empty(n)
    for k in range(MAX_SESSIONS):
        m = horizon == k + 1
        if m.any():
            close_px[m] = _pad(paths["closes"], k, np.nan)[m]
            deadline[m] = _pad(paths["close_minutes"], k, np.inf)[m]

    def first_touch(levels, matrix):
        if levels is None:
            return np.full(n, np.inf)
        idx = np.clip(np.searchsorted(LEVELS, levels - 1e-9), 0, len(LEVELS) - 1)
        t = matrix[np.arange(n), idx]
        return np.where((t < 0) | (levels > LEVELS[-1]), np.inf, t)

    is_long = side > 0
    t_stop = np.where(is_long, first_touch(stop_pct, dn), first_touch(stop_pct, up))
    t_tgt = np.where(is_long, first_touch(target_pct, up), first_touch(target_pct, dn))
    t_stop = np.where(t_stop <= deadline, t_stop, np.inf)
    t_tgt = np.where(t_tgt <= deadline, t_tgt, np.inf)

    stopped = np.isfinite(t_stop) & (t_stop <= t_tgt)
    hit = np.isfinite(t_tgt) & (t_tgt < t_stop)
    held = side * (close_px / entry - 1)
    ret = np.where(stopped, -(stop_pct if stop_pct is not None else 0) / 100,
                   np.where(hit, (target_pct if target_pct is not None else 0) / 100,
                            held))
    reason = np.where(stopped, "sl", np.where(hit, "tp", "close"))
    return ret - COSTS_BP / 1e4, reason


def stage_sweep(args) -> None:
    if not PATHS.exists():
        raise SystemExit("run the `paths` stage first")
    paths = pd.read_parquet(PATHS)
    meta = scored_events(args.effort)[
        ["symbol", "date", "year", "move_pct", "run5d", "confidence",
         "guidance", "quality_flags", "gated"]]
    p = paths.drop(columns=["gated"]).merge(meta, on=["symbol", "date"], how="inner")
    n = len(p)
    g = p["gated"].to_numpy()
    early = p["year"].isin(["2021", "2022", "2023"]).to_numpy()
    print(f"\n{n} scored events with multi-session paths "
          f"({int(g.sum())} gated) · {int(early.sum())} in 2021-23\n")

    lines: list[str] = []

    def emit(text=""):
        print(text)
        lines.append(text)

    def stats(ret, mask):
        r = ret[mask]
        return (r.mean() * 1e4, r[early[mask]].mean() * 1e4,
                r[~early[mask]].mean() * 1e4, float((r > 0).mean()) * 100)

    # ---------------------------------------------------- fixed horizons
    emit("## Fixed holding periods, no bracket (bp per trade)")
    emit()
    emit("| hold | gated | 2021-23 | 2024-26 | win% | vetoed | spread |")
    emit("|---|---|---|---|---|---|---|")
    base = {}
    for k in range(1, MAX_SESSIONS + 1):
        ret, _ = resolve(p, np.full(n, k), None, None)
        base[k] = ret
        gm, ge, gl, gw = stats(ret, g)
        vm = ret[~g].mean() * 1e4
        emit(f"| T+{k} | {gm:+.1f} | {ge:+.1f} | {gl:+.1f} | {gw:.1f} "
             f"| {vm:+.1f} | {gm - vm:+.1f} |")
    emit()

    emit("## Fixed horizons with the shipped-style bracket (5% stop / 2x target)")
    emit()
    emit("| hold | gated | stopped% | target% | ran to close% |")
    emit("|---|---|---|---|---|")
    for k in range(1, MAX_SESSIONS + 1):
        ret, why = resolve(p, np.full(n, k), np.full(n, 5.0), np.full(n, 10.0))
        emit(f"| T+{k} | {ret[g].mean() * 1e4:+.1f} "
             f"| {(why[g] == 'sl').mean() * 100:.1f} "
             f"| {(why[g] == 'tp').mean() * 100:.1f} "
             f"| {(why[g] == 'close').mean() * 100:.1f} |")
    emit()

    # ------------------------------------------------ conditional horizons
    emit("## Conditional holding period")
    emit()
    emit("Base hold T+1, extended to T+K when the rule fires. PEAD's claim is "
         "that drift scales with the size of the surprise and with how much "
         "of it is unabsorbed, so these extend on signal strength and on "
         "confirmation rather than on a fixed clock.")
    emit()
    conf = p["confidence"].to_numpy()
    move = np.abs(p["move_pct"].to_numpy())
    run5 = p["run5d"].to_numpy()
    guid = p["guidance"].to_numpy()
    flags = np.array([bool(x is not None and len(x)) for x in p["quality_flags"]])
    # Did T+1 close in the trade's favour? Confirmation, known at the T+1 close.
    t1_favourable = base[1] > 0

    rules = {
        "always T+1 (shipped horizon)": np.zeros(n, bool),
        "always T+3": np.ones(n, bool),
        "|reaction| >= 10%": move >= 10,
        "high confidence": conf == "high",
        "guidance raised or lowered": np.isin(guid, ["raised", "lowered"]),
        "no quality flags": ~flags,
        "not already priced in (|run5d| < 5%)": np.abs(np.nan_to_num(run5)) < 5,
        "T+1 confirmed (closed in favour)": t1_favourable,
        "T+1 confirmed AND |reaction| >= 10%": t1_favourable & (move >= 10),
        "T+1 confirmed AND high confidence": t1_favourable & (conf == "high"),
    }
    for extend_to in (2, 3, 5):
        emit(f"### extend to T+{extend_to}")
        emit()
        emit("| rule | fires% | gated | 2021-23 | 2024-26 | win% |")
        emit("|---|---|---|---|---|---|")
        for label, fires in rules.items():
            horizon = np.where(fires, extend_to, 1)
            ret, _ = resolve(p, horizon, None, None)
            gm, ge, gl, gw = stats(ret, g)
            emit(f"| {label} | {fires[g].mean() * 100:.0f} | {gm:+.1f} "
                 f"| {ge:+.1f} | {gl:+.1f} | {gw:.1f} |")
        emit()

    emit("Read the two period columns together: a rule that only wins on "
         "2021-23 is fitted to it. `always T+1` and `always T+3` are the "
         "unconditional baselines every rule has to beat.")

    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    DOC.mkdir(parents=True, exist_ok=True)
    header = [f"# Holding period study — {stamp}", "",
              f"{n} scored events ({int(g.sum())} gated), corrected "
              f"per-session exits, {COSTS_BP:.0f}bp round trip. Same-bar "
              f"bracket ties resolve to the stop; a bracket only exits if "
              f"touched before the horizon's close.", ""]
    (DOC / f"holding_period_{stamp}.md").write_text(
        "\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"\nwrote {DOC / f'holding_period_{stamp}.md'}")


def account(p: pd.DataFrame, ret: np.ndarray, horizon: np.ndarray,
            sessions: list, spy_close: np.ndarray, weight: float = 0.10,
            gross_cap: float = 1.0) -> dict:
    """Compound the trades through a real calendar with real capital limits.

    Per-trade basis points cannot answer whether a longer hold is better,
    because a multi-session hold TIES UP the slot: hold five names for three
    days each and the sixth signal has nowhere to go. So exposure is tracked
    per session and a trade that would breach the gross cap is skipped, not
    silently taken. That penalty is exactly what a longer horizon has to pay
    for, and leaving it out is how holding-period studies flatter themselves.
    """
    index = {d: i for i, d in enumerate(sessions)}
    order = np.argsort([index.get(d, 10 ** 9) for d in p["edate"]])
    gross = np.zeros(len(sessions))
    daily = np.zeros(len(sessions))
    taken = skipped = 0
    for j in order:
        entry = index.get(p["edate"].iloc[j])
        if entry is None:
            continue
        exit_i = min(entry + int(horizon[j]), len(sessions) - 1)
        span = slice(entry, exit_i + 1)
        if gross[span].max(initial=0.0) + weight > gross_cap:
            skipped += 1
            continue
        gross[span] += weight
        daily[exit_i] += weight * ret[j]
        taken += 1
    equity = np.cumprod(1 + daily)
    peak = np.maximum.accumulate(equity)
    mdd = float((1 - equity / peak).max())
    spy_ret = np.diff(spy_close, prepend=spy_close[0]) / spy_close
    beta, alpha_d = np.polyfit(spy_ret, daily, 1)
    years = len(sessions) / 252
    return {
        "taken": taken, "skipped": skipped,
        "total_pct": (equity[-1] - 1) * 100,
        "cagr_pct": (equity[-1] ** (1 / years) - 1) * 100,
        "max_dd_pct": mdd * 100,
        "sharpe": float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)),
        "alpha_pct": float(alpha_d * 252 * 100),
        "beta": float(beta),
        "equity": equity,
    }


def stage_best(args) -> None:
    """Joint search over horizon x stop x target, chosen on 2021-23 and
    scored on 2024-26, then compounded through the calendar."""
    from research_llm_contamination import _spy_daily

    if not PATHS.exists():
        raise SystemExit("run the `paths` stage first")
    paths = pd.read_parquet(PATHS)
    meta = scored_events(args.effort)[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "confidence",
         "guidance", "quality_flags", "gated"]]
    p = paths.drop(columns=["gated"]).merge(meta, on=["symbol", "date"], how="inner")
    p = p[p["gated"]].reset_index(drop=True)
    n = len(p)
    early = p["year"].isin(["2021", "2022", "2023"]).to_numpy()
    lo, hi = min(p["edate"]), max(p["edate"])
    spy = _spy_daily(lo, hi + pd.Timedelta(days=14).to_pytimedelta()).sort_values("date")
    sessions = spy["date"].to_list()
    spy_close = spy["close"].to_numpy()

    base1, _ = resolve(p, np.ones(n, int), None, None)
    move = np.abs(p["move_pct"].to_numpy())
    conf = p["confidence"].to_numpy()
    HORIZONS = {"T+1": np.ones(n, int)}
    for k in (2, 3, 4, 5):
        HORIZONS[f"T+{k}"] = np.full(n, k)
    HORIZONS["T+1, T+3 if confirmed"] = np.where(base1 > 0, 3, 1)
    HORIZONS["T+1, T+3 if |move|>=10%"] = np.where(move >= 10, 3, 1)
    HORIZONS["T+1, T+3 if confirmed & high conf"] = np.where(
        (base1 > 0) & (conf == "high"), 3, 1)
    HORIZONS["T+1, T+5 if confirmed"] = np.where(base1 > 0, 5, 1)

    rows = []
    for hname, horizon in HORIZONS.items():
        for stop in (None, 5.0, 8.0, 12.0, 20.0):
            targets = (None, 10.0, 20.0) if stop is None else (None, stop * 2, stop * 3)
            for tgt in targets:
                sp = None if stop is None else np.full(n, stop)
                tp = None if tgt is None else np.full(n, tgt)
                ret, _ = resolve(p, horizon, sp, tp)
                rows.append({
                    "horizon": hname,
                    "stop": "none" if stop is None else f"{stop:g}%",
                    "target": "none" if tgt is None else f"{tgt:g}%",
                    "train": ret[early].mean() * 1e4,
                    "test": ret[~early].mean() * 1e4,
                    "all": ret.mean() * 1e4,
                    "win": float((ret > 0).mean()) * 100,
                    "_ret": ret, "_h": horizon})
    df = pd.DataFrame(rows).sort_values("train", ascending=False).reset_index(drop=True)

    print(f"\n{n} gated events · chosen on 2021-23 ({int(early.sum())}), "
          f"scored on 2024-26 ({int((~early).sum())})\n")
    print(f"{'horizon':32s}{'stop':>7}{'target':>8}{'train':>9}{'test':>9}"
          f"{'all':>9}{'win%':>7}")
    print("-" * 81)
    for _, r in df.head(12).iterrows():
        print(f"{r['horizon']:32s}{r['stop']:>7}{r['target']:>8}"
              f"{r['train']:>+9.1f}{r['test']:>+9.1f}{r['all']:>+9.1f}{r['win']:>7.1f}")
    shipped = df[(df["horizon"] == "T+1") & (df["stop"] == "5%")
                 & (df["target"] == "10%")]
    if len(shipped):
        r = shipped.iloc[0]
        print("-" * 81)
        print(f"{'T+1 / 5% / 2x (SHIPPED SHAPE)':32s}{r['stop']:>7}{r['target']:>8}"
              f"{r['train']:>+9.1f}{r['test']:>+9.1f}{r['all']:>+9.1f}{r['win']:>7.1f}")

    print("\n=== account simulation, 10% notional per name, 100% gross cap ===")
    print(f"{'config':44s}{'total':>9}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}"
          f"{'alpha':>8}{'beta':>7}{'skipped':>9}")
    print("-" * 101)
    picks = list(df.head(3).index) + list(shipped.index[:1])
    results = {}
    for i in picks:
        r = df.loc[i]
        acct = account(p, r["_ret"], r["_h"], sessions, spy_close)
        label = f"{r['horizon']} / {r['stop']} / {r['target']}"
        results[label] = (r, acct)
        print(f"{label:44s}{acct['total_pct']:>+8.1f}%{acct['cagr_pct']:>7.2f}%"
              f"{acct['max_dd_pct']:>7.1f}%{acct['sharpe']:>8.2f}"
              f"{acct['alpha_pct']:>+7.2f}%{acct['beta']:>7.3f}"
              f"{acct['skipped']:>9d}")
    spy_curve = spy_close / spy_close[0]
    peak = np.maximum.accumulate(spy_curve)
    print(f"{'SPY buy & hold':44s}{(spy_curve[-1] - 1) * 100:>+8.1f}%"
          f"{((spy_curve[-1]) ** (252 / len(sessions)) - 1) * 100:>7.2f}%"
          f"{(1 - spy_curve / peak).max() * 100:>7.1f}%")

    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    out = df.drop(columns=["_ret", "_h"])
    out.to_parquet(CACHE / "holding_bracket_grid.parquet")
    lines = [f"# Best-configuration search — {stamp}", "",
             f"{n} gated events. Chosen on 2021-23, scored on 2024-26. "
             f"Account: 10% notional per name, 100% gross cap, skips signals "
             f"that would breach it (the cost a longer hold has to pay).", "",
             "| horizon | stop | target | train | test | all | win% |",
             "|---|---|---|---|---|---|---|"]
    for _, r in out.head(15).iterrows():
        lines.append(f"| {r['horizon']} | {r['stop']} | {r['target']} | "
                     f"{r['train']:+.1f} | {r['test']:+.1f} | {r['all']:+.1f} | "
                     f"{r['win']:.1f} |")
    lines += ["", "| config | total | CAGR | maxDD | Sharpe | alpha | beta | skipped |",
              "|---|---|---|---|---|---|---|---|"]
    for label, (r, a) in results.items():
        lines.append(f"| {label} | {a['total_pct']:+.1f}% | {a['cagr_pct']:.2f}% | "
                     f"{a['max_dd_pct']:.1f}% | {a['sharpe']:.2f} | "
                     f"{a['alpha_pct']:+.2f}% | {a['beta']:.3f} | {a['skipped']} |")
    (DOC / f"best_config_{stamp}.md").write_text("\n".join(lines) + "\n",
                                                 encoding="utf-8")
    print(f"\nwrote {DOC / f'best_config_{stamp}.md'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["paths", "sweep", "best"])
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()
    {"paths": stage_paths, "sweep": stage_sweep,
     "best": stage_best}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
