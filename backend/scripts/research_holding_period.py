"""How long should an LLM-gated earnings-reaction trade be held, and what
should the gate actually do with a verdict?

Four jobs, in order.

0. FILTER THE MIS-TIMED EVENTS. scored_events() drops the 227 of 1,552 whose
   8-K was accepted outside [16:00, 16:20] ET — 75 are not after-close
   releases at all and 152 were not public at the price the study enters at.
   Cause and evidence in research_llm_contamination.timing_ok(); found by
   verify_no_lookahead.py. `--all-events` restores the old behaviour.

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

4. MUTATE THE GATE. The shipped rule trades the tape when the verdict agrees
   and stands down otherwise, which throws the disagreements away — and the
   vetoed bucket returns -257bp, so a confident refusal is a signal, not an
   absence of one. `mutations` scores fifteen ways of spending the same
   verdicts, including the one that decides the question: fading EVERY event
   with no model at all. That control loses 80bp, so the fade is the model
   and not mean reversion.

Caching: ONE Alpaca pass stores, per event, the per-session closes and the
first-touch minute for every excursion level across the whole window. After
that every horizon, every bracket and every conditional rule resolves
offline. No LLM calls — the cached verdicts are reused untouched.

Run: .venv/Scripts/python.exe scripts/research_holding_period.py paths
     .venv/Scripts/python.exe scripts/research_holding_period.py sweep
     .venv/Scripts/python.exe scripts/research_holding_period.py best
     .venv/Scripts/python.exe scripts/research_holding_period.py mutations
     .venv/Scripts/python.exe scripts/research_holding_period.py trades
"""

from __future__ import annotations

import argparse
import json
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
    timing_ok,
)

PATHS = CACHE / "event_paths_multi.parquet"
DOC = Path(__file__).resolve().parents[2] / "docs" / "notes"

LEVELS = np.round(np.arange(0.5, 40.01, 0.5), 2)
MAX_SESSIONS = 5
FETCH_DAYS = 11          # calendar days: enough for 5 sessions over a holiday


def paths_file(panel: str = "v2") -> Path:
    """Where the per-event minute paths live for a given panel."""
    if panel == "v2":
        from research_event_panel import PATHS_V2
        return PATHS_V2
    return PATHS


def scored_events(effort: str, all_events: bool = False,
                  panel: str = "v2", arm: str = "named") -> pd.DataFrame:
    """Every event the study scored — gated AND vetoed. The vetoed leg is
    needed to restate the gate spread on a corrected exit.

    Two panels, both kept so the paper can show what the repair changed:

      v1  the original fixed [16:00, 16:20] ET reaction window. Its `amc`
          classification read a UTC timestamp as ET, so 227 of 1,552 events
          are mis-timed; they are dropped unless `--all-events`.
      v2  the reaction measured in the 15 minutes after EACH event's own
          EDGAR acceptance (research_event_panel). No timing filter is needed
          because the defect the filter worked around is gone, and the events
          it discarded are measured properly instead of thrown away.
    """
    res = _load_results()
    res = res[(res["effort"] == effort) & (res["arm"] == arm)]
    if panel == "v2":
        from research_event_panel import load_universe_v2
        universe = load_universe_v2()
        # `exit` and `fwd_bp` live in the path cache for v2 — the next
        # session's 15:55 close, re-derived rather than read off a stale
        # column.
        paths = pd.read_parquet(paths_file("v2"))
        first_close = {(s, d): c[0] for s, d, c in
                       zip(paths["symbol"], paths["date"], paths["closes"])}
        universe["exit"] = [first_close.get((s, d), np.nan) for s, d
                            in zip(universe["symbol"], universe["date"])]
        universe["fwd_bp"] = (universe["exit"] / universe["react"] - 1) * 1e4
        m = res.merge(universe, on=["symbol", "date"], how="inner")
        m["side"] = np.sign(m["move_pct"]).astype(int)
        m["gated"] = _gate(m)
        m["timing_ok"] = True
        return m[m["exit"].notna()].reset_index(drop=True)

    universe = load_universe().rename(columns={"date": "edate"})
    universe["date"] = universe["edate"].astype(str)
    m = res.merge(universe, on=["symbol", "date"], how="inner")
    m["side"] = np.sign(m["move_pct"]).astype(int)
    m["gated"] = _gate(m)
    m["timing_ok"] = timing_ok(m)
    if not all_events:
        dropped = int((~m["timing_ok"]).sum())
        m = m[m["timing_ok"]].reset_index(drop=True)
        print(f"acceptance-time filter: dropped {dropped} events accepted "
              f"outside [16:00, 16:20] ET, {len(m)} remain")
    return m


def stage_paths(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    m = scored_events(args.effort, args.all_events, args.panel)
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
    pf = paths_file(args.panel)
    if not pf.exists():
        raise SystemExit(f"no {pf.name} — run the `paths` stage first")
    paths = pd.read_parquet(pf)
    meta = scored_events(args.effort, args.all_events, args.panel)[
        ["symbol", "date", "year", "move_pct", "run5d", "confidence",
         "guidance", "quality_flags", "gated"]]
    p = paths.drop(columns=["gated"], errors="ignore").merge(meta, on=["symbol", "date"], how="inner")
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
        "always extend (unconditional)": np.ones(n, bool),
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
            sessions: list, spy_close: np.ndarray, target_gross: float = 0.30,
            max_weight: float = 0.20) -> dict:
    """Compound the trades through a real calendar at constant deployed capital.

    A multi-session hold ties up the slot: hold five names for three days each
    and the account is three times as committed as the same book held one day.
    Comparing horizons at a FIXED per-name weight therefore compares two
    different amounts of risk, and skipping the overflow (the first version of
    this) just converts that into a hidden coverage penalty.

    So size the chunks instead. Measure the average number of concurrent
    positions the schedule actually produces, then set the per-name weight to
    gross_cap / that. A T+3 book holds ~3x as many names at once and each is
    ~1/3 the size, so average deployed capital is equal across horizons and
    every signal is taken. The comparison is then about the RETURN of the
    horizon, not about how much capital it happened to commit.
    """
    index = {d: i for i, d in enumerate(sessions)}
    entries = np.array([index.get(d, -1) for d in p["edate"]])
    live = entries >= 0
    exits = np.minimum(entries + horizon.astype(int), len(sessions) - 1)

    # Pass 1: how many positions are open on an average session?
    occupancy = np.zeros(len(sessions))
    for j in np.where(live)[0]:
        occupancy[entries[j]:exits[j] + 1] += 1
    active = occupancy[occupancy > 0]
    avg_concurrent = float(active.mean()) if len(active) else 1.0
    # target_gross is the AVERAGE deployed capital every horizon is held to.
    # It has to sit below max_weight x avg_concurrent or the per-name cap
    # binds and the normalisation silently stops happening — which is what
    # made the first run report a 100% CAGR off a 20%-per-name book.
    weight = min(target_gross / avg_concurrent, max_weight)

    # Pass 2: P&L lands on the session the position is closed in.
    daily = np.zeros(len(sessions))
    for j in np.where(live)[0]:
        daily[exits[j]] += weight * ret[j]
    equity = np.cumprod(1 + daily)
    peak = np.maximum.accumulate(equity)
    mdd = float((1 - equity / peak).max())
    spy_ret = np.diff(spy_close, prepend=spy_close[0]) / spy_close
    beta, alpha_d = np.polyfit(spy_ret, daily, 1)
    years = len(sessions) / 252
    return {
        "taken": int(live.sum()), "avg_concurrent": round(avg_concurrent, 2),
        "weight_pct": round(weight * 100, 2),
        "peak_concurrent": int(occupancy.max()),
        "daily": daily,
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
    from research_llm_contamination import _iso_return_leverage, _spy_daily

    pf = paths_file(args.panel)
    if not pf.exists():
        raise SystemExit(f"no {pf.name} — run the `paths` stage first")
    paths = pd.read_parquet(pf)
    meta = scored_events(args.effort, args.all_events, args.panel)[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "confidence",
         "guidance", "quality_flags", "gated"]]
    p = paths.drop(columns=["gated"], errors="ignore").merge(meta, on=["symbol", "date"], how="inner")
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
    # The sweep's best conditional rule: guidance is the forward-looking part
    # of a release, and PEAD says drift persists where the surprise is about
    # future earnings rather than the quarter just reported.
    guid = p["guidance"].to_numpy()
    moved_guidance = np.isin(guid, ["raised", "lowered"])
    HORIZONS["T+1, T+3 if guidance moved"] = np.where(moved_guidance, 3, 1)
    HORIZONS["T+1, T+2 if guidance moved"] = np.where(moved_guidance, 2, 1)

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

    # The four series the paper compares. `best` and `alt` are whatever the
    # search chose; `shipped` and `t1plain` are FIXED and drawn whether or
    # not they place well — the point of a baseline is that it appears when
    # it loses. The old code took df.head(3) + shipped and let a shipped
    # config that landed in the top 3 overwrite its own row.
    def config(horizon, stop, target):
        hit = df[(df["horizon"] == horizon) & (df["stop"] == stop)
                 & (df["target"] == target)]
        return hit.iloc[0] if len(hit) else None

    picks: list[tuple[str, pd.Series]] = [("best", df.iloc[0]),
                                          ("alt", df.iloc[1])]
    for key, cfg in (("shipped", config("T+1", "5%", "10%")),
                     ("t1plain", config("T+1", "none", "none"))):
        if cfg is not None:
            picks.append((key, cfg))

    print("\n=== account simulation, 30% average deployed capital ===")
    print(f"{'config':44s}{'total':>9}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}"
          f"{'alpha':>8}{'beta':>7}{'conc':>6}{'wt%':>6}")
    print("-" * 104)
    results = {}
    for key, r in picks:
        acct = account(p, r["_ret"], r["_h"], sessions, spy_close)
        label = f"{r['horizon']} / {r['stop']} / {r['target']}"
        results[key] = (r, acct)
        print(f"{label:38s}{key:>6}{acct['total_pct']:>+8.1f}%"
              f"{acct['cagr_pct']:>7.2f}%"
              f"{acct['max_dd_pct']:>7.1f}%{acct['sharpe']:>8.2f}"
              f"{acct['alpha_pct']:>+7.2f}%{acct['beta']:>7.3f}"
              f"{acct['avg_concurrent']:>6.1f}{acct['weight_pct']:>6.1f}")
    spy_curve = spy_close / spy_close[0]
    peak = np.maximum.accumulate(spy_curve)
    print(f"{'SPY buy & hold':44s}{(spy_curve[-1] - 1) * 100:>+8.1f}%"
          f"{((spy_curve[-1]) ** (252 / len(sessions)) - 1) * 100:>7.2f}%"
          f"{(1 - spy_curve / peak).max() * 100:>7.1f}%")

    # Publish the corrected curves for the deck. The old study-curve.json was
    # built on the mislabeled 2-4 session exit; this replaces it.
    curve_file = (Path(__file__).resolve().parents[2] / "frontend" / "public"
                  / "study-curve.json")
    spy_curve = (spy_close / spy_close[0]).tolist()
    series, stats_out = {"spy": [round(v, 5) for v in spy_curve]}, {}
    names = {}
    # "Is this just index exposure?" is a different question for SPY, for the
    # NASDAQ-100 (earnings reactions concentrate in tech, so QQQ is the
    # benchmark the strategy could plausibly be a proxy for) and for TQQQ —
    # if the returns were levered beta, 3x QQQ is what they would look like,
    # drawdown included. One single-factor regression each, plus the receipt:
    # the smallest daily-rebalanced exposure that ends where the strategy
    # ends, and what its drawdown would have been.
    bench: dict[str, dict] = {}
    for sym in ("SPY", "QQQ", "TQQQ"):
        b = _spy_daily(lo, hi + pd.Timedelta(days=14).to_pytimedelta(), sym)
        px = dict(zip(b["date"], b["close"]))
        line, last = [], None
        for d in sessions:
            last = px.get(d, last)
            line.append(last)
        if line[0] is None:
            continue
        bench[sym] = {"curve": [v / line[0] for v in line],
                      "ret": [0.0] + [line[i] / line[i - 1] - 1
                                      for i in range(1, len(line))]}

    for key, r in picks:
        _, a = results[key]
        series[key] = [round(v, 5) for v in a["equity"]]
        names[key] = f"{r['horizon']} / stop {r['stop']} / target {r['target']}"
        stats_out[key] = {k: v for k, v in a.items()
                          if k not in ("equity", "daily")}
        daily = np.asarray(a["daily"])
        stats_out[key]["vs"], stats_out[key]["iso"] = {}, {}
        for sym, b in bench.items():
            beta, alpha_d = np.polyfit(np.asarray(b["ret"]), daily, 1)
            stats_out[key]["vs"][sym] = {
                "alpha_annual_pct": round(float(alpha_d) * 252 * 100, 2),
                "beta": round(float(beta), 3),
                "corr": round(float(np.corrcoef(np.asarray(b["ret"]), daily)[0, 1]), 3)}
            stats_out[key]["iso"][sym] = _iso_return_leverage(
                b["ret"], a["equity"][-1])
    for sym, b in bench.items():
        if sym == "SPY":
            continue
        c = np.asarray(b["curve"])
        pk = np.maximum.accumulate(c)
        rr = np.asarray(b["ret"])
        series[sym.lower()] = [round(float(v), 5) for v in c]
        names[sym.lower()] = f"{sym} buy & hold"
        stats_out[sym.lower()] = {
            "total_pct": float((c[-1] - 1) * 100),
            "cagr_pct": float((c[-1] ** (252 / len(sessions)) - 1) * 100),
            "max_dd_pct": float((1 - c / pk).max()) * 100,
            "sharpe": float(rr.mean() / rr.std(ddof=1) * np.sqrt(252)),
            "alpha_pct": 0.0,
            "beta": round(float(np.polyfit(np.asarray(bench["SPY"]["ret"]),
                                           rr, 1)[0]), 3)}
    peak = np.maximum.accumulate(np.asarray(spy_curve))
    stats_out["spy"] = {
        "total_pct": (spy_curve[-1] - 1) * 100,
        "cagr_pct": (spy_curve[-1] ** (252 / len(sessions)) - 1) * 100,
        "max_dd_pct": float((1 - np.asarray(spy_curve) / peak).max()) * 100,
        "sharpe": float(np.mean(np.diff(spy_close) / spy_close[:-1])
                        / np.std(np.diff(spy_close) / spy_close[:-1], ddof=1)
                        * np.sqrt(252)),
        "alpha_pct": 0.0, "beta": 1.0}
    names["spy"] = "SPY buy & hold"
    curve_file.write_text(json.dumps({
        "updated": datetime.now(ET).isoformat(), "model": "claude-opus-5",
        "effort": args.effort, "corrected": True,
        "span": [str(sessions[0]), str(sessions[-1])],
        "note": ("corrected next-session exits; 30% average deployed capital, "
                 "chunk-normalised across horizons; 13bp round trip"),
        "dates": [str(d) for d in sessions],
        "labels": names, "series": series, "stats": stats_out,
    }), encoding="utf-8")
    print(f"wrote {curve_file}")

    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    out = df.drop(columns=["_ret", "_h"])
    out.to_parquet(CACHE / "holding_bracket_grid.parquet")
    lines = [f"# Best-configuration search — {stamp}", "",
             f"{n} gated events. Chosen on 2021-23, scored on 2024-26. "
             f"Account: per-name weight set to 100% gross / average "
             f"concurrent positions, so every horizon deploys the same "
             f"average capital and no signal is skipped.", "",
             "| horizon | stop | target | train | test | all | win% |",
             "|---|---|---|---|---|---|---|"]
    for _, r in out.head(15).iterrows():
        lines.append(f"| {r['horizon']} | {r['stop']} | {r['target']} | "
                     f"{r['train']:+.1f} | {r['test']:+.1f} | {r['all']:+.1f} | "
                     f"{r['win']:.1f} |")
    lines += ["", "| config | total | CAGR | maxDD | Sharpe | alpha | beta | "
              "avg concurrent | weight |",
              "|---|---|---|---|---|---|---|---|---|"]
    for label, (r, a) in results.items():
        lines.append(f"| {label} | {a['total_pct']:+.1f}% | {a['cagr_pct']:.2f}% | "
                     f"{a['max_dd_pct']:.1f}% | {a['sharpe']:.2f} | "
                     f"{a['alpha_pct']:+.2f}% | {a['beta']:.3f} | "
                     f"{a['avg_concurrent']:.1f} | {a['weight_pct']:.1f}% |")
    (DOC / f"best_config_{stamp}.md").write_text("\n".join(lines) + "\n",
                                                 encoding="utf-8")
    print(f"\nwrote {DOC / f'best_config_{stamp}.md'}")


# --------------------------------------------------------------- mutations

def _verdict_side(direction: str) -> int:
    return {"bullish": 1, "bearish": -1}.get(str(direction), 0)


def _field_side(value: str, kind: str) -> int:
    """The direction a schema field points, or 0 for 'no opinion'.

    `inline`, `not_stated`, `maintained` and `none_given` are genuinely
    silent, not neutral-leaning: requiring agreement with a silent field
    would veto on absence of evidence.
    """
    if kind == "guidance":
        return {"raised": 1, "lowered": -1}.get(str(value), 0)
    return {"beat": 1, "miss": -1}.get(str(value), 0)


def mutation_sides(p: pd.DataFrame) -> dict[str, np.ndarray]:
    """Per-event side vectors, +1 long / -1 short / 0 stand down.

    The live gate is one point in this space: trade the tape when the verdict
    agrees, otherwise nothing. That throws away the disagreements, and the
    study says the vetoed bucket returns -196 to -283bp — a confident
    disagreement is a signal, not an absence of one. These mutations spend
    that information different ways so the table can say which part of the
    edge is the model, which is the tape, and which is the refusal.
    """
    tape = np.sign(p["move_pct"].to_numpy()).astype(int)
    llm = np.array([_verdict_side(d) for d in p["direction"]])
    conf = p["confidence"].to_numpy()
    flags = np.array([bool(x is not None and len(x)) for x in p["quality_flags"]])
    agree = (llm != 0) & (llm == tape)          # the shipped gate
    disagree = (llm != 0) & (llm != tape)       # the vetoed-with-a-view bucket
    neutral = llm == 0
    eps = np.array([_field_side(v, "eps") for v in p["eps_vs_consensus"]])
    rev = np.array([_field_side(v, "rev") for v in p["revenue_vs_consensus"]])
    guid = np.array([_field_side(v, "guidance") for v in p["guidance"]])
    hi = conf == "high"
    himed = np.isin(conf, ["high", "medium"])

    def only(mask, side):
        return np.where(mask, side, 0)

    return {
        # --- the three baselines every mutation has to beat ---------------
        "SHIPPED gate (verdict agrees with tape)": only(agree, tape),
        "pure tape (mechanical, ungated)": tape,
        # THE CONTROL THAT DECIDES THE FADE. Every fade mutation bets against
        # the tape, so "reversal works on 5% earnings moves" would produce the
        # same result with no model at all. This row is that null. If fading
        # everything loses and fading the model's refusals wins, the
        # difference is the model.
        "anti-tape baseline (fade EVERY event, no model)": -tape,
        # --- spend the refusals ------------------------------------------
        "fade only: trade every disagreement": only(disagree, llm),
        "fade only: high confidence": only(disagree & hi, llm),
        "fade only: high|medium confidence": only(disagree & himed, llm),
        "pure LLM direction (gate + fade, tape ignored)": llm,
        "gate + fade on high confidence": only(agree | (disagree & hi), llm),
        # --- the abstentions ---------------------------------------------
        "neutral verdicts, traded with the tape": only(neutral, tape),
        "neutral verdicts, traded against the tape": only(neutral, -tape),
        # --- the maximal mutation: never stand down -----------------------
        "fade every non-agreement (disagree OR neutral)": only(~agree, -tape),
        "FULL POLICY: with the tape if the verdict agrees, against it "
        "otherwise": np.where(agree, tape, -tape),
        # --- which schema field carries the edge? -------------------------
        "gate AND eps agrees": only(agree & (eps == llm), tape),
        "gate AND revenue agrees": only(agree & (rev == llm), tape),
        "gate AND eps AND revenue agree": only(agree & (eps == llm) & (rev == llm), tape),
        "gate AND guidance agrees": only(agree & (guid == llm), tape),
        # --- quality flags: live shrinks size 0.75x; try harder uses ------
        "gate AND no quality flags (hard veto)": only(agree & ~flags, tape),
        "gate, quality flags INVERT the side": np.where(
            agree, np.where(flags, -tape, tape), 0),
    }


def _split_stats(ret: np.ndarray, side: np.ndarray, early: np.ndarray) -> dict:
    live = side != 0
    r = ret[live]
    e = early[live]
    if not len(r):
        return {"n": 0, "all": 0.0, "train": 0.0, "test": 0.0, "win": 0.0,
                "t": 0.0}
    t = (r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))) if len(r) > 1 else 0.0
    return {"n": int(live.sum()),
            "all": r.mean() * 1e4,
            "train": (r[e].mean() * 1e4) if e.any() else float("nan"),
            "test": (r[~e].mean() * 1e4) if (~e).any() else float("nan"),
            "win": float((r > 0).mean()) * 100,
            "t": float(t)}


def stage_mutations(args) -> None:
    """Score every gate mutation on the 2021-23 / 2024-26 split, at two
    horizons, with the account model. Most of these are expected to fail;
    they are reported anyway, because a table of only the winners is a
    selection effect with a bibliography."""
    from research_llm_contamination import _spy_daily

    pf = paths_file(args.panel)
    if not pf.exists():
        raise SystemExit(f"no {pf.name} — run the `paths` stage first")
    paths = pd.read_parquet(pf)
    meta = scored_events(args.effort, args.all_events, args.panel)[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "direction",
         "confidence", "eps_vs_consensus", "revenue_vs_consensus", "guidance",
         "quality_flags", "gated"]]
    p = paths.drop(columns=["gated", "side"], errors="ignore").merge(
        meta, on=["symbol", "date"], how="inner").reset_index(drop=True)
    n = len(p)
    early = p["year"].isin(["2021", "2022", "2023"]).to_numpy()
    lo, hi = min(p["edate"]), max(p["edate"])
    spy = _spy_daily(lo, hi + pd.Timedelta(days=14).to_pytimedelta()).sort_values("date")
    sessions = spy["date"].to_list()
    spy_close = spy["close"].to_numpy()

    sides = mutation_sides(p)
    lines: list[str] = []

    def emit(text=""):
        print(text)
        lines.append(text)

    emit(f"{n} scored events, {int(early.sum())} in 2021-23 / "
         f"{int((~early).sum())} in 2024-26. Costs {COSTS_BP:.0f}bp round "
         f"trip. No bracket unless stated.")
    emit()
    emit("## Every mutation, unbracketed, at two horizons")
    emit()
    emit("| mutation | trades | % of book | T+1 all | 21-23 | 24-26 | win% | t "
         "| T+3 all | 21-23 | 24-26 |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    scored_rows = {}
    for label, side in sides.items():
        row = {}
        for k in (1, 3):
            ret, _ = resolve(p.assign(side=side), np.full(n, k), None, None)
            row[k] = _split_stats(ret, side, early)
            row[f"ret{k}"] = ret
        scored_rows[label] = (side, row)
        a, b = row[1], row[3]
        emit(f"| {label} | {a['n']} | {a['n'] / n * 100:.0f} | {a['all']:+.1f} "
             f"| {a['train']:+.1f} | {a['test']:+.1f} | {a['win']:.1f} "
             f"| {a['t']:+.2f} | {b['all']:+.1f} | {b['train']:+.1f} "
             f"| {b['test']:+.1f} |")
    emit()
    emit("Read the two horizon blocks against each other. The vetoed bucket "
         "gets monotonically worse with holding period while the gated one is "
         "flat, so if the refusals really contain a tradeable signal the fade "
         "must IMPROVE from T+1 to T+3. That is the single prediction this "
         "table exists to test.")
    emit()

    # ------------------------------------------------------ the fade, in depth
    emit("## The fade, under scrutiny")
    emit()
    emit("This is the mutation that would change the strategy, so it gets the "
         "hostile treatment: does it survive a wider spread, does it hold in "
         "both halves, and is it one side of the book or both?")
    emit()
    fade_side = sides["fade only: trade every disagreement"]
    live = fade_side != 0
    emit("| holding period | trades | mean bp | 2021-23 | 2024-26 | win% | t |")
    emit("|---|---|---|---|---|---|---|")
    for k in (1, 2, 3, 4, 5):
        ret, _ = resolve(p.assign(side=fade_side), np.full(n, k), None, None)
        s = _split_stats(ret, fade_side, early)
        emit(f"| T+{k} | {s['n']} | {s['all']:+.1f} | {s['train']:+.1f} "
             f"| {s['test']:+.1f} | {s['win']:.1f} | {s['t']:+.2f} |")
    emit()
    emit("### Cost sensitivity")
    emit()
    emit("Fading a 5%+ after-hours move means crossing the book in the "
         f"direction nobody wants. The study's flat {COSTS_BP:.0f}bp is the "
         "friendliest assumption available; these are the same trades priced "
         "progressively worse.")
    emit()
    emit("| round-trip cost | T+1 | T+3 | T+3 2024-26 |")
    emit("|---|---|---|---|")
    r1, _ = resolve(p.assign(side=fade_side), np.ones(n, int), None, None)
    r3, _ = resolve(p.assign(side=fade_side), np.full(n, 3), None, None)
    for cost in (13.0, 25.0, 40.0, 60.0, 100.0):
        adj = (COSTS_BP - cost) / 1e4
        a = _split_stats(r1 + adj, fade_side, early)
        b = _split_stats(r3 + adj, fade_side, early)
        emit(f"| {cost:.0f}bp | {a['all']:+.1f} | {b['all']:+.1f} "
             f"| {b['test']:+.1f} |")
    emit()
    emit("### By confidence, by side, by year")
    emit()
    emit("| slice | trades | T+1 | T+3 | win% (T+3) |")
    emit("|---|---|---|---|---|")
    tape = np.sign(p["move_pct"].to_numpy()).astype(int)
    slices = {
        "high confidence": live & (p["confidence"] == "high").to_numpy(),
        "medium confidence": live & (p["confidence"] == "medium").to_numpy(),
        "low confidence": live & (p["confidence"] == "low").to_numpy(),
        "fading an UP tape (short)": live & (tape > 0),
        "fading a DOWN tape (long)": live & (tape < 0),
        "|reaction| >= 10%": live & (np.abs(p["move_pct"].to_numpy()) >= 10),
    }
    for year in sorted(p["year"].unique()):
        slices[f"year {year}"] = live & (p["year"] == year).to_numpy()
    for label, mask in slices.items():
        sub = np.where(mask, fade_side, 0)
        a = _split_stats(r1, sub, early)
        b = _split_stats(r3, sub, early)
        emit(f"| {label} | {a['n']} | {a['all']:+.1f} | {b['all']:+.1f} "
             f"| {b['win']:.1f} |")
    emit()

    # ------------------------------------------------------ account model
    emit("## Account model")
    emit()
    emit("Every mutation compounded through the real calendar at 30% average "
         "deployed capital, per-name weight normalised by the average number "
         "of concurrent positions so a longer hold does not silently commit "
         "more capital. T+1, unbracketed.")
    emit()
    emit("| mutation | trades | total | CAGR | maxDD | Sharpe | alpha | beta |")
    emit("|---|---|---|---|---|---|---|---|")
    acct_rows = {}
    for label, (side, row) in scored_rows.items():
        live_m = side != 0
        if live_m.sum() < 30:
            continue
        sub = p[live_m].reset_index(drop=True)
        a = account(sub, row["ret1"][live_m], np.ones(int(live_m.sum()), int),
                    sessions, spy_close)
        acct_rows[label] = a
        emit(f"| {label} | {a['taken']} | {a['total_pct']:+.1f}% "
             f"| {a['cagr_pct']:.2f}% | {a['max_dd_pct']:.1f}% "
             f"| {a['sharpe']:.2f} | {a['alpha_pct']:+.2f}% | {a['beta']:.3f} |")
    emit()
    spy_curve = spy_close / spy_close[0]
    peak = np.maximum.accumulate(spy_curve)
    emit(f"SPY buy & hold over the same span: {(spy_curve[-1] - 1) * 100:+.1f}% "
         f"total, {(spy_curve[-1] ** (252 / len(sessions)) - 1) * 100:.2f}% "
         f"CAGR, {(1 - spy_curve / peak).max() * 100:.1f}% max drawdown.")
    emit()
    emit("Quality flags fire on "
         f"{np.mean([bool(x is not None and len(x)) for x in p['quality_flags']]) * 100:.0f}% "
         "of releases, so the live 0.75x size shrink is very nearly a constant "
         "and cannot be doing discriminating work.")

    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    DOC.mkdir(parents=True, exist_ok=True)
    header = [f"# Gate mutations — {stamp}", "",
              "The shipped gate trades the tape when the verdict agrees and "
              "stands down otherwise. Each mutation below spends the same "
              "verdicts differently. Chosen on nothing — every row is "
              "reported, including the failures.", ""]
    doc = DOC / f"gate_mutations_{stamp}.md"
    doc.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"\nwrote {doc}")

    out = pd.DataFrame([
        {"mutation": k, "trades": v[1][1]["n"],
         "t1_all": v[1][1]["all"], "t1_train": v[1][1]["train"],
         "t1_test": v[1][1]["test"], "t1_win": v[1][1]["win"],
         "t3_all": v[1][3]["all"], "t3_train": v[1][3]["train"],
         "t3_test": v[1][3]["test"],
         **{f"acct_{kk}": vv for kk, vv in acct_rows.get(k, {}).items()
            if kk != "equity"}}
        for k, v in scored_rows.items()])
    out.to_parquet(CACHE / "gate_mutations.parquet")
    print(f"wrote {CACHE / 'gate_mutations.parquet'}")


TRADES_FILE = (Path(__file__).resolve().parents[2] / "frontend" / "public"
               / "study-trades.json")


def stage_trades(args) -> None:
    """Export every scored event — taken AND declined — for the LAB deck's
    trade explorer.

    The declined ones are the point. This strategy's value is refusal, so a
    view that only shows executed trades hides the thing that makes money.
    Each row carries the model's verdict, whether the gate let it through,
    why not when it didn't, and what the trade would have returned under each
    exit policy."""
    pf = paths_file(args.panel)
    if not pf.exists():
        raise SystemExit(f"no {pf.name} — run the `paths` stage first")
    paths = pd.read_parquet(pf)
    meta = scored_events(args.effort, args.all_events, args.panel)
    p = paths.drop(columns=["gated"], errors="ignore").merge(
        meta[["symbol", "date", "edate", "year", "move_pct", "run5d", "dv",
              "direction", "confidence", "guidance", "quality_flags",
              "summary", "gated", "anchor", "react"]],
        on=["symbol", "date"], how="inner")
    n = len(p)

    policies = {
        "t1": (np.ones(n, int), None, None),
        "t3": (np.full(n, 3), None, None),
        "shipped": (np.ones(n, int), np.full(n, 5.0), np.full(n, 10.0)),
    }
    out = {}
    for name, (h, s, t) in policies.items():
        ret, why = resolve(p, h, s, t)
        out[name] = (ret * 1e4, why)

    def reason_not_taken(row) -> str:
        if row["direction"] == "neutral":
            return "verdict neutral — no directional call"
        return ("verdict bullish, tape down" if row["direction"] == "bullish"
                else "verdict bearish, tape up")

    rows = []
    for i, (_, r) in enumerate(p.iterrows()):
        rows.append({
            "sym": r["symbol"], "date": r["date"], "year": r["year"],
            "side": int(r["side"]), "move": round(float(r["move_pct"]), 2),
            "run5d": (None if pd.isna(r["run5d"]) else round(float(r["run5d"]), 2)),
            "dvM": (None if pd.isna(r["dv"]) else round(float(r["dv"]) / 1e6)),
            "anchor": round(float(r["anchor"]), 2), "entry": round(float(r["react"]), 2),
            "dir": r["direction"], "conf": r["confidence"], "guid": r["guidance"],
            "flags": list(r["quality_flags"] or []),
            "summary": str(r["summary"])[:400],
            "gated": bool(r["gated"]),
            "why": "" if r["gated"] else reason_not_taken(r),
            "ret": {k: round(float(v[0][i]), 1) for k, v in out.items()},
            "exit": {k: str(v[1][i]) for k, v in out.items()},
        })
    rows.sort(key=lambda x: (x["date"], x["sym"]))
    payload = {
        "updated": datetime.now(ET).isoformat(),
        "effort": args.effort, "costs_bp": COSTS_BP,
        "policies": {"t1": "hold to T+1 close", "t3": "hold to T+3 close",
                     "shipped": "T+1 with the shipped 5% stop / 2x target"},
        "n": len(rows), "gated": int(sum(r["gated"] for r in rows)),
        "trades": rows,
    }
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRADES_FILE.write_text(json.dumps(payload), encoding="utf-8")
    print(f"wrote {TRADES_FILE} ({TRADES_FILE.stat().st_size / 1024:.0f} KB) — "
          f"{payload['n']} events, {payload['gated']} taken")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["paths", "sweep", "best", "trades",
                                      "mutations"])
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--panel", default="v2", choices=["v1", "v2"],
                    help="v2 = acceptance-relative reaction windows "
                         "(default); v1 = the original fixed 16:00-16:20 slice")
    ap.add_argument("--all-events", action="store_true",
                    help="keep events whose 8-K was accepted outside "
                         "[16:00, 16:20] ET (the pre-2026-08-06 behaviour)")
    args = ap.parse_args()
    {"paths": stage_paths, "sweep": stage_sweep, "best": stage_best,
     "trades": stage_trades, "mutations": stage_mutations}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
