"""Wick-outs, filtered stops, and the head-fake question, at minute resolution.

THE ASK (Matthew, 2026-08-10). Earnings nights are binary and violent: a naive
stop gets wicked out by the first burst of two-sided tape, but trading them
naked leaves the whole tail. Three questions, in rising order of ambition:

  1. ANATOMY   How often does a stop at X% kill a trade that would have WON
               the horizon anyway (the wick-out rate), and what does that cost
               against what the stop saves on true losers?
  2. MECHANICS Do filtered stops — confirmation on bar closes, dwell time,
               arming delays, RTH-only, VWAP-referenced, trailing — keep the
               protection while dodging the wicks? And does a take-profit at
               the event's own expected move beat a fixed percent?
  3. READING   Can the first 30-60 minutes of SIP tape after the release
               classify "permanent repricing" vs "initial overreaction that
               reverts" well enough to act on — via interpretable filters, a
               walk-forward classifier, and a small fitted-Q policy (the RL
               that ~2k events can support without memorising them)?

THE PANEL is the paper's own: event_paths_v2 — 1,962 |reaction|>=5% AMC
events 2016-2026, top-5-per-night by liquidity, $50M floor, entered at the
last print 15 minutes after each event's own EDGAR acceptance. Two side
vectors get every battery:

  tape   sign of the reaction — the mechanical arm, no LLM anywhere.
  FULL   the paper's never-stand-down policy (with the tape if the verdict
         agrees, against it otherwise) on the scored subset.

EXPECTED MOVE, point-in-time: median |reaction| of the symbol's last four
PRIOR events across the full ungated react caches (min 2), falling back to
the expanding global median. No options data before 2024 exists on this
tier, so the straddle-implied move is proxied by realized reaction history —
the live engine can substitute the true pre-print straddle when it has one.

FILLS. The cached grid records first-touch minutes per 0.5% level, so grid
stops fill AT the level (optimistic through gaps; ties go to the stop). The
minute stages re-resolve stops at the NEXT bar's close after the breach —
a stop-market with one minute of latency — and the note reports both, plus
a slippage stress on stop fills. Targets are resting limits and fill at the
level in both resolvers. Costs: 13bp round trip everywhere (the study's
budget), restated at 23.2bp (the measured AH fill) in the summary.

NO LOOKAHEAD. Every rule acts on information at or before the minute it
fires. Features for the reading stage use tape from acceptance to the
decision minute only; the classifier and the Q-policy are trained strictly
walk-forward (fit through year T-1, act on year T).

THE HEALTH WARNING. Stage 2 sweeps ~150 rule variants and stage 3 fits
models; the winner of any search this size is partly luck. Everything is
split train (2016-23) / test (2024-26); a rule whose halves disagree in sign
is noise no matter the headline. The daily sweep already learned this: its
best rule (10% target) was best-of-171 and is quarantined from live.

Run:  python scripts/research_wickout.py anatomy
      python scripts/research_wickout.py rules
      python scripts/research_wickout.py fetch      # ~25 min, resumable
      python scripts/research_wickout.py mrules
      python scripts/research_wickout.py learn
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime, timedelta

from _paths import BACKEND, DATA, NOTES, SCRIPTS

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_holding_period import (  # noqa: E402
    FULL_MUT,
    LEVELS,
    SLOTS,
    _pad,
    account_slots,
    mutation_sides,
    paths_file,
    resolve,
    scored_events,
)
from research_llm_contamination import CACHE, COSTS_BP, ET, _spy_daily  # noqa: E402

MINUTES_F = CACHE / "wick_minutes.parquet"
STRESS_BP = 23.2          # the measured AH round trip, for the restatement
STOP_SLIP_BP = 15.0       # extra pain on a stop fill in fast tape, stress only
TRAIN_YEARS = ("2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023")


def stamp() -> str:
    return datetime.now(ET).strftime("%Y%m%d_%H%M")


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


# ------------------------------------------------------------------- panel

def em_proxy(sym_dates: list[tuple[str, str]]) -> np.ndarray:
    """Point-in-time expected move per (symbol, date): median |reaction|% of
    the symbol's last <=4 PRIOR events in the full ungated react caches,
    falling back to the expanding global median when history is thin."""
    frames = [pd.read_parquet(f, columns=["symbol", "date", "anchor", "react"])
              for f in sorted(CACHE.glob("events_v2_*.parquet"))]
    hist = (pd.concat(frames, ignore_index=True)
            .drop_duplicates(["symbol", "date"]).sort_values("date"))
    hist["m"] = (hist["react"] / hist["anchor"] - 1).abs() * 100
    by_sym: dict[str, tuple[list, list]] = {
        s: (g["date"].to_list(), g["m"].to_list())
        for s, g in hist.groupby("symbol")}
    gdates = hist["date"].to_numpy()
    gmoves = hist["m"].to_numpy()
    out = np.full(len(sym_dates), np.nan)
    for i, (s, d) in enumerate(sym_dates):
        ds, ms = by_sym.get(s, ([], []))
        j = int(np.searchsorted(ds, d))          # events strictly before d
        prior = ms[max(0, j - 4):j]
        if len(prior) >= 2:
            out[i] = float(np.median(prior))
        else:
            k = int(np.searchsorted(gdates, d))
            if k >= 50:
                out[i] = float(np.median(gmoves[:k]))
    return out


def panel() -> pd.DataFrame:
    """event_paths_v2 with meta (move, liquidity, run-up), the EM proxy, and
    edate — the mechanical panel. Sides here are the TAPE sign."""
    from research_event_panel import load_universe_v2

    paths = pd.read_parquet(paths_file("v2"))
    meta = load_universe_v2()[
        ["symbol", "date", "edate", "move_pct", "run5d", "dv"]]
    p = paths.merge(meta, on=["symbol", "date"], how="inner").reset_index(drop=True)
    p["year"] = p["date"].str[:4]
    p["em"] = em_proxy(list(zip(p["symbol"], p["date"])))
    p["em"] = p["em"].fillna(float(np.nanmedian(p["em"])))
    return p


def full_sides(p: pd.DataFrame) -> np.ndarray:
    """The paper's FULL POLICY side per panel row, 0 where unscored."""
    m = scored_events("medium", False, "v2")[
        ["symbol", "date", "move_pct", "direction", "confidence",
         "eps_vs_consensus", "revenue_vs_consensus", "guidance",
         "quality_flags"]]
    side = mutation_sides(m).get(FULL_MUT)
    lut = {(s, d): v for s, d, v in zip(m["symbol"], m["date"], side)}
    return np.array([lut.get((s, d), 0) for s, d
                     in zip(p["symbol"], p["date"])], dtype=int)


def spy_frame(p: pd.DataFrame):
    lo, hi = min(p["edate"]), max(p["edate"])
    spy = _spy_daily(lo, hi + timedelta(days=14)).sort_values("date")
    return spy["date"].to_list(), spy["close"].to_numpy()


def adverse_grids(p: pd.DataFrame, side: np.ndarray):
    """First-touch matrices oriented to the POSITION: adv = against it,
    fav = with it. Rows with side 0 are garbage — mask them upstream."""
    up = np.stack(p["up"].to_numpy())
    dn = np.stack(p["dn"].to_numpy())
    is_long = side > 0
    adv = np.where(is_long[:, None], dn, up)
    fav = np.where(is_long[:, None], up, dn)
    return adv, fav


def touched_by(matrix: np.ndarray, deadline: np.ndarray) -> np.ndarray:
    """Boolean (n, len(LEVELS)): level touched at or before each event's
    deadline minute."""
    t = np.where(matrix < 0, np.inf, matrix)
    return t <= deadline[:, None]


def horizon_close(p: pd.DataFrame, k: int) -> tuple[np.ndarray, np.ndarray]:
    return (_pad(p["closes"], k - 1, np.nan),
            _pad(p["close_minutes"], k - 1, np.inf))


# ----------------------------------------------------------------- anatomy

def stage_anatomy(args) -> None:
    p = panel()
    side = p["side"].to_numpy()
    entry = p["entry"].to_numpy()
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Wick anatomy — how deep do winners cut before they pay — {stamp()}")
    emit()
    emit(f"{len(p)} events (|reaction|>=5%, top-5/night, 2016-2026), tape "
         f"sides, entry at accept+15min. MAE/MFE from the cached minute "
         f"first-touch grid; net{COSTS_BP:.0f}bp hold-to-close returns.")
    emit()

    for k, label in ((1, "T+1 close"), (3, "T+3 close")):
        cpx, dline = horizon_close(p, k)
        held = side * (cpx / entry - 1) - COSTS_BP / 1e4
        adv, fav = adverse_grids(p, side)
        tby = touched_by(adv, dline)
        # deepest adverse level touched before the deadline, in % and in EM
        deepest = np.where(tby.any(1), LEVELS[np.maximum(
            tby.shape[1] - 1 - np.argmax(tby[:, ::-1], axis=1), 0)], 0.0)
        deepest = np.where(tby.any(1), deepest, 0.0)
        win = held > 0
        emit(f"## Horizon {label}: hold-to-close wins {win.mean()*100:.1f}%, "
             f"mean {held.mean()*1e4:+.1f}bp")
        emit()
        emit("Max adverse excursion BEFORE the horizon close, by final outcome:")
        emit()
        emit("| percentile | winners MAE | losers MAE | winners MAE/EM | losers MAE/EM |")
        emit("|---|---|---|---|---|")
        em = p["em"].to_numpy()
        for q in (50, 75, 90, 95):
            emit(f"| p{q} | {np.percentile(deepest[win], q):.1f}% "
                 f"| {np.percentile(deepest[~win], q):.1f}% "
                 f"| {np.percentile((deepest/em)[win], q):.2f} "
                 f"| {np.percentile((deepest/em)[~win], q):.2f} |")
        emit()
        emit("P(touch adverse s before close) and P(win | touched) — the "
             "recovery odds a stop at s forfeits:")
        emit()
        emit("| s | touched% | of winners touched% | P(win given touched) | "
             "mean bp given touched | P(win given NOT touched) |")
        emit("|---|---|---|---|---|---|")
        for s in (1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
            i = int(np.searchsorted(LEVELS, s - 1e-9))
            t = tby[:, i]
            emit(f"| {s:.0f}% | {t.mean()*100:.1f} | {t[win].mean()*100:.1f} "
                 f"| {win[t].mean()*100:.1f}% | {held[t].mean()*1e4:+.1f} "
                 f"| {win[~t].mean()*100:.1f}% |")
        emit()
        emit("Same, with depth in units of the event's own expected move:")
        emit()
        emit("| s/EM | touched% | P(win given touched) | mean bp given touched |")
        emit("|---|---|---|---|")
        for r in (0.25, 0.5, 0.75, 1.0, 1.5):
            lv = np.clip(np.round(r * em * 2) / 2, LEVELS[0], LEVELS[-1])
            idx = np.searchsorted(LEVELS, lv - 1e-9)
            t = tby[np.arange(len(p)), idx]
            emit(f"| {r:.2f} | {t.mean()*100:.1f} | {win[t].mean()*100:.1f}% "
                 f"| {held[t].mean()*1e4:+.1f} |")
        emit()

    out = NOTES / f"wick_anatomy_{stamp()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# ------------------------------------------------------------------- rules

def bracket_battery(p: pd.DataFrame, side: np.ndarray, label: str,
                    lines: list[str]) -> pd.DataFrame:
    """Every grid-resolvable bracket on one side vector: fixed and EM-scaled
    stops/targets over T+1 and T+3, with the wick decomposition."""
    n = len(p)
    live = side != 0
    yr = p["year"].to_numpy()
    train = np.isin(yr, TRAIN_YEARS)
    em = p["em"].to_numpy()
    sessions, spy_close = spy_frame(p)
    q = p.assign(side=side)

    def emlv(mult):
        return np.clip(np.round(mult * em * 2) / 2, LEVELS[0], LEVELS[-1])

    stops: dict[str, np.ndarray | None] = {"-": None}
    for s in (2.0, 3.0, 5.0, 8.0, 12.0):
        stops[f"{s:g}%"] = np.full(n, s)
    for r in (0.25, 0.5, 0.75):
        stops[f"{r:g}xEM"] = emlv(r)
    tgts: dict[str, np.ndarray | None] = {"-": None}
    for t in (5.0, 8.0, 10.0, 15.0, 20.0):
        tgts[f"{t:g}%"] = np.full(n, t)
    for r in (0.5, 1.0, 1.5):
        tgts[f"{r:g}xEM"] = emlv(r)

    rows = []
    for k in (1, 3):
        hz = np.full(n, k)
        base, _ = resolve(q, hz, None, None)
        for sname, sv in stops.items():
            for tname, tv in tgts.items():
                ret, why = resolve(q, hz, sv, tv)
                m = live
                stopped = (why == "sl") & m
                hit = (why == "tp") & m
                wick = stopped & (base > 0)
                r = ret[m]

                def sh(mask):
                    mm = m & mask
                    if mm.sum() < 30:
                        return np.nan
                    return account_slots(p[mm].reset_index(drop=True),
                                         ret[mm], hz[mm], sessions, spy_close,
                                         n_slots=SLOTS)["sharpe"]
                rows.append({
                    "hz": f"T+{k}", "stop": sname, "tgt": tname,
                    "bp": r.mean() * 1e4,
                    "bp_train": ret[m & train].mean() * 1e4,
                    "bp_test": ret[m & ~train].mean() * 1e4,
                    "t_test": tstat(ret[m & ~train]),
                    "win%": (r > 0).mean() * 100,
                    "stop%": stopped.sum() / m.sum() * 100,
                    "tp%": hit.sum() / m.sum() * 100,
                    "wick%": (wick.sum() / max(stopped.sum(), 1)) * 100,
                    "dbp": (ret[m] - base[m]).mean() * 1e4,
                    "sh_train": sh(train), "sh_test": sh(~train)})
    df = pd.DataFrame(rows)

    lines.append(f"## {label} — {int(live.sum())} live events")
    lines.append("")
    lines.append("Top 15 by TEST Sharpe (2024-26), grid fills (stop at level, "
                 "ties to the stop):")
    lines.append("")
    lines.append("| hz | stop | tgt | bp | train | test | t(test) | win% | "
                 "stop% | tp% | wick% | dbp vs hold | Sh tr | Sh te |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    top = df.sort_values("sh_test", ascending=False).head(15)
    naked = df[(df["stop"] == "-") & (df["tgt"] == "-")]
    show = pd.concat([top, naked]).drop_duplicates(["hz", "stop", "tgt"])
    for _, r in show.iterrows():
        lines.append(
            f"| {r['hz']} | {r['stop']} | {r['tgt']} | {r['bp']:+.1f} "
            f"| {r['bp_train']:+.1f} | {r['bp_test']:+.1f} | {r['t_test']:+.2f} "
            f"| {r['win%']:.0f} | {r['stop%']:.0f} | {r['tp%']:.0f} "
            f"| {r['wick%']:.0f} | {r['dbp']:+.1f} "
            f"| {r['sh_train']:.2f} | {r['sh_test']:.2f} |")
    lines.append("")
    return df


def stage_rules(args) -> None:
    p = panel()
    lines: list[str] = [
        f"# Bracket battery on the first-touch grid — {stamp()}", "",
        f"Fixed and expected-move-scaled stops/targets, net {COSTS_BP:.0f}bp. "
        f"EM = median |reaction| of the symbol's last 4 prior events "
        f"(point-in-time; median EM {np.nanmedian(p['em']):.1f}%, p10 "
        f"{np.nanpercentile(p['em'], 10):.1f}%, p90 "
        f"{np.nanpercentile(p['em'], 90):.1f}%). wick% = share of stop-outs "
        f"that would have WON their horizon unbracketed. dbp = mean uplift vs "
        f"the same horizon held naked. Train 2016-23, test 2024-26.", ""]
    tape = p["side"].to_numpy()
    df_tape = bracket_battery(p, tape, "TAPE sides (mechanical)", lines)
    fs = full_sides(p)
    df_full = bracket_battery(p, fs, "FULL POLICY sides (LLM, scored subset)",
                              lines)
    df_tape.assign(arm="tape").pipe(
        lambda a: pd.concat([a, df_full.assign(arm="full")])
    ).to_csv(DATA / "wick_rules_grid.csv", index=False)
    lines.append("full grid -> data/wick_rules_grid.csv")
    out = NOTES / f"wick_rules_{stamp()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# ------------------------------------------------------------------- fetch

def stage_fetch(args) -> None:
    """Minute OHLCV per event: acceptance minute through the FIRST next
    session's 20:00 ET, batched one request per announce night. Resumable."""
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    p = panel()
    prior, have = pd.DataFrame(), set()
    if MINUTES_F.exists():
        prior = pd.read_parquet(MINUTES_F)
        have = set(zip(prior["symbol"], prior["date"]))
    todo = p[[(s, d) not in have for s, d in zip(p["symbol"], p["date"])]]
    print(f"{len(todo)} of {len(p)} events need minute paths")
    if not len(todo):
        return

    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows, flushed = [], len(prior)
    days = sorted(todo["date"].unique())
    for n_day, day in enumerate(days):
        g = todo[todo["date"] == day]
        d = datetime.fromisoformat(day)
        syms = sorted(g["symbol"].unique())
        bars_by_sym: dict[str, list] = {}
        for i in range(0, len(syms), 100):
            try:
                res = api.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=syms[i:i + 100],
                    timeframe=TimeFrame.Minute,
                    start=d.replace(hour=15, minute=30, tzinfo=ET),
                    end=(d + timedelta(days=4)).replace(
                        hour=20, minute=5, tzinfo=ET),
                    feed="sip"))
                bars_by_sym.update(res.data)
            except Exception as exc:                       # noqa: BLE001
                print(f"  {day} chunk {i}: {str(exc)[:70]}")
                _time.sleep(1.0)
            _time.sleep(0.25)
        for _, e in g.iterrows():
            bars = bars_by_sym.get(e["symbol"]) or []
            if not bars:
                continue
            acc = int(e["acc_min"])
            dayoff, mod, c, h, lo_, v = [], [], [], [], [], []
            t1_date = None
            for b in bars:
                ts = b.timestamp.astimezone(ET)
                off = (ts.date() - d.date()).days
                hm = ts.hour * 60 + ts.minute
                if off == 0 and hm < acc:
                    continue
                if off > 0 and t1_date is None:
                    t1_date = ts.date()
                if t1_date is not None and ts.date() > t1_date:
                    break
                dayoff.append(off)
                mod.append(hm)
                c.append(float(b.close))
                h.append(float(b.high))
                lo_.append(float(b.low))
                v.append(float(b.volume))
            if not c:
                continue
            rows.append({
                "symbol": e["symbol"], "date": day, "acc_min": acc,
                "react_min": int(e["react_min"]), "entry": float(e["entry"]),
                "dayoff": np.array(dayoff, dtype=np.int8),
                "mod": np.array(mod, dtype=np.int16),
                "c": np.array(c, dtype=np.float32),
                "h": np.array(h, dtype=np.float32),
                "l": np.array(lo_, dtype=np.float32),
                "v": np.array(v, dtype=np.float32)})
        if n_day % 25 == 0 or n_day == len(days) - 1:
            print(f"  {n_day + 1}/{len(days)} nights ({day}), "
                  f"{flushed + len(rows)} events cached")
        if len(rows) >= 400:
            allf = (pd.concat([pd.read_parquet(MINUTES_F),
                               pd.DataFrame(rows)], ignore_index=True)
                    if MINUTES_F.exists() else pd.DataFrame(rows))
            allf.to_parquet(MINUTES_F)
            flushed = len(allf)
            rows = []
    if rows:
        allf = (pd.concat([pd.read_parquet(MINUTES_F), pd.DataFrame(rows)],
                          ignore_index=True)
                if MINUTES_F.exists() else pd.DataFrame(rows))
        allf.to_parquet(MINUTES_F)
        flushed = len(allf)
    print(f"cached {flushed} minute paths -> {MINUTES_F}")


# ---------------------------------------------------------------- minutes

def load_minutes(p: pd.DataFrame) -> pd.DataFrame:
    mm = pd.read_parquet(MINUTES_F)
    q = p.merge(mm.drop(columns=["acc_min", "react_min", "entry"]),
                on=["symbol", "date"], how="inner").reset_index(drop=True)
    return q


class Path1:
    """One event's minute path, oriented to the position (ret arrays are
    side-signed percents vs entry)."""

    __slots__ = ("side", "entry", "i_entry", "i_t1open", "i_t1close",
                 "rc", "rh", "rl", "c", "v", "dayoff", "mod", "n", "t1_close")

    def __init__(self, row, side: int):
        self.side = side
        self.entry = float(row["entry"])
        self.dayoff = row["dayoff"]
        self.mod = row["mod"]
        self.c = row["c"].astype(np.float64)
        self.v = row["v"].astype(np.float64)
        h = row["h"].astype(np.float64)
        low = row["l"].astype(np.float64)
        e = self.entry
        if side > 0:
            self.rc = (self.c / e - 1) * 100
            self.rh = (h / e - 1) * 100
            self.rl = (low / e - 1) * 100
        else:
            self.rc = (1 - self.c / e) * 100
            self.rh = (1 - low / e) * 100          # best excursion for shorts
            self.rl = (1 - h / e) * 100
        self.n = len(self.c)
        react = int(row["react_min"])
        d0 = self.dayoff == self.dayoff.min()
        after = np.where(d0 & (self.mod >= react))[0]
        self.i_entry = int(after[0]) if len(after) else 0
        d1 = self.dayoff > 0
        rth1 = np.where(d1 & (self.mod >= 570) & (self.mod <= 955))[0]
        self.i_t1open = int(rth1[0]) if len(rth1) else self.n - 1
        self.i_t1close = int(rth1[-1]) if len(rth1) else self.n - 1
        self.t1_close = float(self.c[self.i_t1close])


def build_paths(q: pd.DataFrame, side: np.ndarray) -> list[Path1 | None]:
    out = []
    for i, (_, row) in enumerate(q.iterrows()):
        out.append(Path1(row, int(side[i])) if side[i] != 0 else None)
    return out


def _fill_ret(pp: Path1, j: int | None, terminal: float,
              at_level: float | None = None) -> tuple[float, str]:
    """Side-signed % return for an exit at bar j (next-close fill), at a
    level (limit fill), or at the terminal if j is None."""
    if j is None:
        return terminal, "close"
    if at_level is not None:
        return at_level, "lvl"
    return float(pp.rc[min(j, pp.n - 1)]), "bar"


def run_rule(paths: list[Path1 | None], rule, terminal: str = "t1close"):
    """Apply a rule(pp) -> (exit_bar or None, level_fill or None, reason) to
    every path. Returns side-signed DECIMAL returns net of COSTS_BP and the
    reason array."""
    rets = np.full(len(paths), np.nan)
    reasons = np.full(len(paths), "", dtype=object)
    for i, pp in enumerate(paths):
        if pp is None:
            continue
        t_end = pp.i_t1close if terminal == "t1close" else pp.i_t1open
        term_ret = float(pp.rc[t_end])
        j, lvl, why = rule(pp, t_end)
        if j is not None and j >= t_end:
            j, lvl, why = None, None, "close"
        r, _ = _fill_ret(pp, j, term_ret, lvl)
        rets[i] = r / 100 - COSTS_BP / 1e4
        reasons[i] = why if j is not None else "close"
    return rets, reasons


# rule factories ------------------------------------------------------------

def r_hold():
    return lambda pp, t_end: (None, None, "close")


def r_hard_stop(s, fill_level=False, arm_from=None, rth_only=False):
    def rule(pp, t_end):
        lo = pp.rl
        start = pp.i_entry if arm_from is None else arm_from(pp)
        for j in range(start, t_end):
            if rth_only and not 570 <= pp.mod[j] <= 959:
                continue
            if lo[j] <= -s:
                if fill_level:
                    return j, -s, "sl"
                return j + 1, None, "sl"
        return None, None, ""
    return rule


def r_confirm_stop(s, k):
    """Exit only when the k-minute AGGREGATED bar closes beyond -s; fill next
    minute. Aggregation is k consecutive stored bars, which on thin AH tape
    spans more wall-clock — a feature: confirmation needs k actual prints."""
    def rule(pp, t_end):
        for j in range(pp.i_entry + k - 1, t_end, 1):
            if (j - pp.i_entry + 1) % k:
                continue
            if pp.rc[j] <= -s:
                return j + 1, None, "sl"
        return None, None, ""
    return rule


def r_dwell_stop(s, m):
    """m consecutive minute closes beyond -s, exit at the m-th close."""
    def rule(pp, t_end):
        run = 0
        for j in range(pp.i_entry, t_end):
            run = run + 1 if pp.rc[j] <= -s else 0
            if run >= m:
                return j + 1, None, "sl"
        return None, None, ""
    return rule


def r_vwap_stop(band_bp, buffer_min, confirm=5):
    """Exit when `confirm` consecutive closes sit beyond the running
    post-acceptance VWAP by band_bp against the position, armed after
    buffer_min stored bars."""
    def rule(pp, t_end):
        pv = np.cumsum(pp.c * pp.v)
        vv = np.cumsum(pp.v)
        vwap = np.where(vv > 0, pv / np.maximum(vv, 1e-9), pp.c)
        sgn = 1.0 if pp.side > 0 else -1.0
        edge = sgn * (pp.c / vwap - 1) * 1e4       # bp above vwap, signed
        run = 0
        for j in range(pp.i_entry + buffer_min, t_end):
            run = run + 1 if edge[j] <= -band_bp else 0
            if run >= confirm:
                return j + 1, None, "sl"
        return None, None, ""
    return rule


def r_trail(d, arm_at=0.0):
    """Trailing stop d% off the running favorable extreme, armed once MFE
    reaches arm_at%."""
    def rule(pp, t_end):
        peak, armed = 0.0, arm_at <= 0.0
        for j in range(pp.i_entry, t_end):
            peak = max(peak, pp.rh[j])
            if not armed and peak >= arm_at:
                armed = True
            if armed and pp.rl[j] <= peak - d:
                return j + 1, None, "sl"
        return None, None, ""
    return rule


def r_take_profit(t):
    def rule(pp, t_end):
        for j in range(pp.i_entry, t_end):
            if pp.rh[j] >= t:
                return j, t, "tp"
        return None, None, ""
    return rule


def combine(stop_rule, tp_rule):
    """First trigger wins; same-bar ties go to the stop."""
    def rule(pp, t_end):
        js, ls, ws = stop_rule(pp, t_end)
        jt, lt, wt = tp_rule(pp, t_end)
        if js is not None and (jt is None or js <= jt):
            return js, ls, ws
        if jt is not None:
            return jt, lt, wt
        return None, None, ""
    return rule


def run_reentry(paths, s):
    """Hard stop at -s, then re-enter (same side) if a later close re-crosses
    the original entry before T+1 14:30. Return is the sum of both legs; the
    second round trip pays its own costs."""
    rets = np.full(len(paths), np.nan)
    took2 = np.zeros(len(paths), bool)
    for i, pp in enumerate(paths):
        if pp is None:
            continue
        t_end = pp.i_t1close
        term = float(pp.rc[t_end])
        j_stop = None
        for j in range(pp.i_entry, t_end):
            if pp.rl[j] <= -s:
                j_stop = j + 1
                break
        if j_stop is None or j_stop >= t_end:
            rets[i] = term / 100 - COSTS_BP / 1e4
            continue
        leg1 = float(pp.rc[min(j_stop, pp.n - 1)])
        j_re = None
        for j in range(j_stop, t_end):
            if pp.dayoff[j] > 0 and pp.mod[j] > 870:
                break
            if pp.rc[j] >= 0.0:
                j_re = j
                break
        if j_re is None:
            rets[i] = leg1 / 100 - COSTS_BP / 1e4
        else:
            leg2 = term - float(pp.rc[j_re])
            rets[i] = (leg1 + leg2) / 100 - 2 * COSTS_BP / 1e4
            took2[i] = True
    return rets, took2


def stage_mrules(args) -> None:
    p = panel()
    q = load_minutes(p)
    side = q["side"].to_numpy()
    if getattr(args, "sides", "tape") == "full":
        side = full_sides(q)
    yr = q["year"].to_numpy()
    train = np.isin(yr, TRAIN_YEARS)
    em = q["em"].to_numpy()
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Filtered stops on real minute tape "
         f"({getattr(args, 'sides', 'tape').upper()} sides) — {stamp()}")
    emit()
    emit(f"{int((side != 0).sum())} live of {len(q)} events with minute "
         f"paths (release evening through T+1 close, SIP). Entry accept+15m, "
         f"terminal T+1 close, net {COSTS_BP:.0f}bp; stops fill at the NEXT "
         f"minute close after the trigger (targets at the level). "
         f"Train 2016-23 / test 2024-26.")
    emit()

    paths = build_paths(q, side)
    base, _ = run_rule(paths, r_hold())
    ok = np.isfinite(base)

    # cross-check the minute terminal against the grid's T+1 close
    cpx, _ = horizon_close(q, 1)
    grid_t1 = side * (cpx / q["entry"].to_numpy() - 1) - COSTS_BP / 1e4
    drift = np.nanmedian(np.abs(base - grid_t1)) * 1e4
    emit(f"terminal sanity: median |minute T+1 - grid T+1| = {drift:.1f}bp")
    emit()

    def emlv(mult):
        return np.clip(np.round(mult * em * 2) / 2, 0.5, 40.0)

    def score(name, rets, n_rt=1):
        r = rets[ok]
        early = np.isfinite(rets) & (np.abs(rets - base) > 1e-12)
        wick = early & (base > 0) & (rets < base)
        stress = rets - (STRESS_BP - COSTS_BP) * n_rt / 1e4 \
            - np.where(early, STOP_SLIP_BP / 1e4, 0.0)
        row = {"rule": name, "bp": np.nanmean(r) * 1e4,
               "train": np.nanmean(rets[ok & train]) * 1e4,
               "test": np.nanmean(rets[ok & ~train]) * 1e4,
               "t_test": tstat(rets[ok & ~train]),
               "test_stress": np.nanmean(stress[ok & ~train]) * 1e4,
               "win%": np.nanmean(r > 0) * 100,
               "early%": early[ok].mean() * 100,
               "wick%": wick.sum() / max(early.sum(), 1) * 100,
               "dbp": np.nanmean((rets - base)[ok]) * 1e4}
        rows.append(row)
        return row

    rows: list[dict] = []
    score("hold to T+1 close", base)
    open_ret, _ = run_rule(paths, r_hold(), terminal="t1open")
    score("exit at T+1 open", open_ret)

    # families ------------------------------------------------------------
    per_event_rules: list[tuple[str, object]] = []
    for s in (2.0, 3.0, 5.0, 8.0):
        per_event_rules += [
            (f"hard stop {s:g}% (next-close fill)", r_hard_stop(s)),
            (f"hard stop {s:g}% (level fill)", r_hard_stop(s, fill_level=True)),
            (f"confirm 5m close < -{s:g}%", r_confirm_stop(s, 5)),
            (f"confirm 15m close < -{s:g}%", r_confirm_stop(s, 15)),
            (f"dwell 5min beyond -{s:g}%", r_dwell_stop(s, 5)),
            (f"dwell 10min beyond -{s:g}%", r_dwell_stop(s, 10)),
            (f"stop {s:g}% armed after 30m",
             r_hard_stop(s, arm_from=lambda pp: pp.i_entry + 30)),
            (f"stop {s:g}% armed after 60m",
             r_hard_stop(s, arm_from=lambda pp: pp.i_entry + 60)),
            (f"stop {s:g}% RTH-only", r_hard_stop(s, rth_only=True)),
        ]
    for bb in (0.0, 25.0, 50.0):
        per_event_rules.append(
            (f"vwap stop band {bb:.0f}bp (5m confirm, 30m arm)",
             r_vwap_stop(bb, 30)))
    for d in (3.0, 5.0, 8.0):
        per_event_rules.append((f"trail {d:g}% from MFE", r_trail(d)))
        per_event_rules.append((f"trail {d:g}% armed at +3%", r_trail(d, 3.0)))
    for name, rule in per_event_rules:
        rets, _ = run_rule(paths, rule)
        score(name, rets)

    # EM-scaled stop mechanics: one loop per event since s varies ---------
    for mult, mname in ((0.25, "0.25xEM"), (0.5, "0.5xEM")):
        for maker, fam in ((lambda s: r_hard_stop(s), "hard"),
                           (lambda s: r_confirm_stop(s, 15), "confirm15"),
                           (lambda s: r_dwell_stop(s, 10), "dwell10")):
            rets = np.full(len(paths), np.nan)
            for i, pp in enumerate(paths):
                if pp is None:
                    continue
                rr, _ = run_rule([pp], maker(float(emlv(mult)[i])))
                rets[i] = rr[0]
            score(f"{fam} stop at {mname}", rets)

    # take profit at the expected move, alone and with the good stops -----
    tp_fixed = [(f"tp {t:g}%", r_take_profit(t)) for t in (5.0, 8.0, 10.0)]
    for name, rule in tp_fixed:
        rets, _ = run_rule(paths, rule)
        score(name + " (no stop)", rets)
    for mult in (0.5, 0.75, 1.0, 1.5):
        rets = np.full(len(paths), np.nan)
        for i, pp in enumerate(paths):
            if pp is None:
                continue
            rr, _ = run_rule([pp], r_take_profit(float(emlv(mult)[i])))
            rets[i] = rr[0]
        score(f"tp {mult:g}xEM (no stop)", rets)
    combos = [("tp 1xEM + confirm15 0.5xEM",
               lambda s_, t_: combine(r_confirm_stop(s_, 15),
                                      r_take_profit(t_)), 0.5, 1.0),
              ("tp 1xEM + hard 0.5xEM",
               lambda s_, t_: combine(r_hard_stop(s_), r_take_profit(t_)),
               0.5, 1.0),
              ("tp 1.5xEM + confirm15 0.5xEM",
               lambda s_, t_: combine(r_confirm_stop(s_, 15),
                                      r_take_profit(t_)), 0.5, 1.5)]
    for name, mk, smult, tmult in combos:
        rets = np.full(len(paths), np.nan)
        sv, tv = emlv(smult), emlv(tmult)
        for i, pp in enumerate(paths):
            if pp is None:
                continue
            rr, _ = run_rule([pp], mk(float(sv[i]), float(tv[i])))
            rets[i] = rr[0]
        score(name, rets)

    # stop-then-reenter ---------------------------------------------------
    for s in (3.0, 5.0):
        rets, took2 = run_reentry(paths, s)
        r = score(f"stop {s:g}% + re-enter at entry (2nd RT charged)", rets)
        r["rule"] += f" [re-entered {took2[ok].mean()*100:.0f}%]"

    df = pd.DataFrame(rows)
    emit("wick% = early exits that killed a would-be winner AND did worse "
         "than holding, as a share of all early exits. test(stress) restates "
         f"2024-26 at the measured {STRESS_BP:.1f}bp round trip plus "
         f"{STOP_SLIP_BP:.0f}bp extra on every early-exit fill.")
    emit()
    emit("| rule | bp | train | test | t(test) | test(stress) | win% "
         "| early% | wick% | dbp |")
    emit("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df.sort_values("test", ascending=False).iterrows():
        emit(f"| {r['rule']} | {r['bp']:+.1f} | {r['train']:+.1f} "
             f"| {r['test']:+.1f} | {r['t_test']:+.2f} "
             f"| {r['test_stress']:+.1f} | {r['win%']:.0f} "
             f"| {r['early%']:.0f} | {r['wick%']:.0f} | {r['dbp']:+.1f} |")
    tag = getattr(args, "sides", "tape")
    df.to_csv(DATA / f"wick_mrules_{tag}.csv", index=False)
    out = NOTES / f"wick_mrules_{tag}_{stamp()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# -------------------------------------------------------------------- learn

def features_at(pp: Path1, minutes_after: int | None, at_open: bool,
                em: float, move: float, run5: float, dv: float) -> dict | None:
    """Feature vector from tape in [acceptance, tau]. tau = entry bar +
    minutes_after stored bars, or the T+1 09:25 premarket mark."""
    if at_open:
        pm = np.where((pp.dayoff > 0) & (pp.mod < 565))[0]
        tau = int(pm[-1]) if len(pm) else None
        if tau is None:
            return None
    else:
        tau = min(pp.i_entry + minutes_after, pp.n - 1)
    if tau <= pp.i_entry:
        return None
    seg = slice(0, tau + 1)
    c, v = pp.c[seg], pp.v[seg]
    rc = pp.rc[seg]
    pv = float((c * v).sum())
    vv = float(v.sum())
    vwap = pv / vv if vv > 0 else float(c[-1])
    sgn = 1.0 if pp.side > 0 else -1.0
    ret_now = float(rc[-1])
    mfe = float(pp.rh[seg].max())
    mae = float(pp.rl[seg].min())
    jump = abs(move)
    # fraction of the initial reaction handed back: 0 = holding the reaction
    # print, +1 = fully round-tripped it, negative = extended beyond it
    retrace = -ret_now / max(jump, 1e-9)
    last15 = float(rc[-1] - rc[max(0, len(rc) - 16)])
    return {
        "ret_now": ret_now, "ret_vs_em": ret_now / max(em, 0.5),
        "retrace": float(np.clip(retrace, -2, 3)),
        "mfe": mfe, "mae": mae, "mae_vs_em": mae / max(em, 0.5),
        "range_used": (mfe - mae) / max(jump, 1e-9),
        "vwap_edge_bp": sgn * (float(c[-1]) / vwap - 1) * 1e4,
        "above_vwap_frac": float(np.mean(sgn * (c / (np.cumsum(c * v)
                                  / np.maximum(np.cumsum(v), 1e-9)) - 1) > 0)),
        "dollar_vol_ratio": pv / max(dv, 1e6),
        "nbars": float(len(c)), "last15": last15,
        "move": jump, "move_vs_em": jump / max(em, 0.5),
        "run5d": float(0.0 if np.isnan(run5) else run5),
        "logdv": float(np.log10(max(dv, 1e6))),
    }


def stage_learn(args) -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler

    p = panel()
    q = load_minutes(p)
    side = q["side"].to_numpy()
    paths = build_paths(q, side)
    em = q["em"].to_numpy()
    move = q["move_pct"].to_numpy()
    run5 = q["run5d"].to_numpy()
    dv = q["dv"].to_numpy()
    yr = q["year"].to_numpy().astype(int)
    base, _ = run_rule(paths, r_hold())            # net T+1 close, decimal

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Reading the first hour: head-fake vs repricing — {stamp()}")
    emit()
    emit(f"{len(q)} events. Target: does the reaction direction still hold at "
         f"the T+1 close (side-signed net return > 0)? Features are SIP tape "
         f"from acceptance to the decision minute only. Walk-forward: fit "
         f"through year T-1 (first test year 2019), predict year T.")
    emit()

    y = (base > 0).astype(int)
    cpx3, _ = horizon_close(q, 3)
    base3 = side * (cpx3 / q["entry"].to_numpy() - 1) - COSTS_BP / 1e4
    y3 = (base3 > 0).astype(int)

    def wf_probs(Xv, yy, yrs):
        pr = np.full(len(yy), np.nan)
        for t in range(2019, 2027):
            tr, te = yrs < t, yrs == t
            if te.sum() == 0 or tr.sum() < 200:
                continue
            gb = HistGradientBoostingClassifier(
                max_depth=3, max_iter=150, learning_rate=0.08,
                min_samples_leaf=40, random_state=7).fit(Xv[tr], yy[tr])
            pr[te] = gb.predict_proba(Xv[te])[:, 1]
        return pr
    for tag, minutes_after, at_open in (
            ("accept+30m", 30, False), ("accept+60m", 60, False),
            ("T+1 09:25 premarket", None, True)):
        feats, keep = [], []
        for i, pp in enumerate(paths):
            if pp is None or not np.isfinite(base[i]):
                continue
            f = features_at(pp, minutes_after, at_open, em[i], move[i],
                            run5[i], dv[i])
            if f is None:
                continue
            feats.append(f)
            keep.append(i)
        X = pd.DataFrame(feats)
        keep = np.array(keep)
        yy = y[keep]
        yrs = yr[keep]
        names = list(X.columns)
        Xv = np.nan_to_num(X.to_numpy(), nan=0.0, posinf=0.0, neginf=0.0)

        emit(f"## Decision at {tag} ({len(keep)} events)")
        emit()
        # univariate: rank-AUC of each feature OOS-pooled (2019+)
        te_all = yrs >= 2019
        emit("single features, AUC vs T+1 continuation (2019-26 pooled):")
        emit()
        uni = []
        for j, nm in enumerate(names):
            try:
                a = roc_auc_score(yy[te_all], Xv[te_all, j])
            except ValueError:
                a = np.nan
            uni.append((nm, a))
        uni.sort(key=lambda x: -abs(x[1] - 0.5))
        emit("| feature | AUC | | feature | AUC |")
        emit("|---|---|---|---|---|")
        for a, b in zip(uni[:7], uni[7:14] + [("", np.nan)] * 7):
            emit(f"| {a[0]} | {a[1]:.3f} | | {b[0]} | "
                 f"{'' if np.isnan(b[1]) else f'{b[1]:.3f}'} |")
        emit()

        # walk-forward models
        prob_lr = np.full(len(keep), np.nan)
        for t in range(2019, 2027):
            tr = yrs < t
            te = yrs == t
            if te.sum() == 0 or tr.sum() < 200:
                continue
            sc = StandardScaler().fit(Xv[tr])
            lr = LogisticRegression(C=0.5, max_iter=2000).fit(
                sc.transform(Xv[tr]), yy[tr])
            prob_lr[te] = lr.predict_proba(sc.transform(Xv[te]))[:, 1]
        prob_gb = wf_probs(Xv, yy, yrs)
        oos = np.isfinite(prob_lr)
        auc_lr = roc_auc_score(yy[oos], prob_lr[oos])
        auc_gb = roc_auc_score(yy[oos], prob_gb[oos])
        emit(f"walk-forward OOS AUC vs T+1 continuation: logistic "
             f"{auc_lr:.3f}, GBM {auc_gb:.3f} "
             f"(n={int(oos.sum())}, base rate {yy[oos].mean()*100:.1f}%)")
        y3k = y3[keep]
        prob3 = wf_probs(Xv, y3k, yrs)
        o3 = np.isfinite(prob3)
        emit(f"same features vs T+3 continuation: GBM AUC "
             f"{roc_auc_score(y3k[o3], prob3[o3]):.3f} "
             f"(base rate {y3k[o3].mean()*100:.1f}%)")
        emit()
        emit("| year | n | AUC(gbm) | | year | n | AUC(gbm) |")
        emit("|---|---|---|---|---|---|---|")
        ylist = [t for t in range(2019, 2027)
                 if np.isfinite(prob_gb[yrs == t]).any()]
        half = (len(ylist) + 1) // 2
        for a, b in zip(ylist[:half], ylist[half:] + [None] * half):
            am = (yrs == a) & oos
            sa = (f"| {a} | {am.sum()} | "
                  f"{roc_auc_score(yy[am], prob_gb[am]):.3f} |"
                  if len(np.unique(yy[am])) > 1 else f"| {a} | {am.sum()} | - |")
            if b is None:
                emit(sa + " | | | |")
                continue
            bm = (yrs == b) & oos
            sb = (f" | {b} | {bm.sum()} | "
                  f"{roc_auc_score(yy[bm], prob_gb[bm]):.3f} |"
                  if len(np.unique(yy[bm])) > 1 else f" | {b} | {bm.sum()} | - |")
            emit(sa + sb)
        emit()

        # policy value: act on the GBM probability. Thresholds fixed a
        # priori — exit the bottom quartile-ish, ride the rest.
        rr = base[keep]
        for plo in (0.35, 0.45):
            cut = oos & (prob_gb < plo)
            # exiting at tau: realize the tape return at tau instead of T+1
            tau_ret = np.array([
                (paths[k].rc[min(paths[k].i_entry + (minutes_after or 0),
                                 paths[k].n - 1)] / 100 - COSTS_BP / 1e4)
                if not at_open else
                (paths[k].rc[paths[k].i_t1open] / 100 - COSTS_BP / 1e4)
                for k in keep])
            pol = np.where(cut, tau_ret, rr)
            emit(f"policy exit-if-P<{plo:.2f}: fires {cut.sum()/oos.sum()*100:.0f}%, "
                 f"policy {np.nanmean(pol[oos])*1e4:+.1f}bp vs hold "
                 f"{np.nanmean(rr[oos])*1e4:+.1f}bp "
                 f"(on fired: exit {np.nanmean(tau_ret[cut])*1e4:+.1f} vs "
                 f"hold {np.nanmean(rr[cut])*1e4:+.1f}bp, n={int(cut.sum())})")
        emit()

    # ---------------------------------------------------------- fitted-Q
    emit("## Fitted-Q exit policy (tabular, 15-minute decision ticks)")
    emit()
    emit("State = (session phase, pnl-vs-EM bucket, giveback-from-MFE "
         "bucket); actions hold/exit. One-backup fitted Q: V(hold) is the "
         "realized terminal return of train episodes passing through the "
         "state, V(exit) the exit-now mark — i.e. a conditional-expectation "
         "table answering 'from this state, did bailing beat holding?'. "
         "Fit on <=2023, played greedily on 2024-26. This is the RL ~2k "
         "events can support; anything deeper memorises the panel.")
    emit()

    TICK = 15
    PHASES = {0: "evening", 1: "premkt", 2: "d1am", 3: "d1pm"}

    def phase_of(pp, j):
        if pp.dayoff[j] == pp.dayoff.min():
            return 0
        if pp.mod[j] < 570:
            return 1
        return 2 if pp.mod[j] < 780 else 3

    def bucket(pp, j, em_i):
        r = pp.rc[j] / max(em_i, 0.5)
        b_pnl = int(np.digitize(r, [-0.75, -0.25, 0.0, 0.25, 0.75]))
        mfe = pp.rh[:j + 1].max()
        giveback = (mfe - pp.rc[j]) / max(mfe, 0.5) if mfe > 0 else 0.0
        b_gb = int(np.digitize(giveback, [0.33, 0.8]))
        return phase_of(pp, j), b_pnl, b_gb

    def episodes(idx):
        eps = []
        for i in idx:
            pp = paths[i]
            if pp is None or not np.isfinite(base[i]):
                continue
            ticks = list(range(pp.i_entry, pp.i_t1close, TICK))
            if not ticks:
                continue
            states = [bucket(pp, j, em[i]) for j in ticks]
            exits = [pp.rc[j] / 100 - COSTS_BP / 1e4 for j in ticks]
            eps.append((states, exits, base[i]))
        return eps

    tr_idx = np.where(yr <= 2023)[0]
    te_idx = np.where(yr >= 2024)[0]
    val: dict[tuple, list] = {}
    for states, exits, term in episodes(tr_idx):
        for s_, x_ in zip(states, exits):
            val.setdefault(s_, [[], []])
            val[s_][0].append(term)           # value of holding to the end
            val[s_][1].append(x_)             # value of exiting here
    Q = {s_: (float(np.mean(v[0])), float(np.mean(v[1])))
         for s_, v in val.items() if len(v[0]) >= 30}
    n_exit_states = sum(1 for h, x in Q.values() if x > h)
    emit(f"{len(Q)} states with >=30 train visits; policy exits in "
         f"{n_exit_states} of them:")
    emit()
    emit("| state (phase, pnl/EM, giveback) | visits | V(hold) bp | V(exit) bp |")
    emit("|---|---|---|---|")
    vis = {s_: len(v[0]) for s_, v in val.items()}
    for s_, (h, x) in sorted(Q.items(), key=lambda kv: kv[1][1] - kv[1][0],
                             reverse=True):
        if x > h:
            emit(f"| {PHASES[s_[0]]}, pnl{s_[1]}, gb{s_[2]} | {vis[s_]} "
                 f"| {h*1e4:+.1f} | {x*1e4:+.1f} |")
    emit()
    pol_ret, act_ret = [], []
    for states, exits, term in episodes(te_idx):
        r = term
        for s_, x_ in zip(states, exits):
            h, x_q = Q.get(s_, (1.0, 0.0))
            if x_q > h:
                r = x_
                break
        pol_ret.append(r)
        act_ret.append(term)
    pol_ret, act_ret = np.array(pol_ret), np.array(act_ret)
    emit(f"2024-26 replay: Q-policy {pol_ret.mean()*1e4:+.1f}bp/trade "
         f"(t {tstat(pol_ret):+.2f}) vs hold-to-T+1 "
         f"{act_ret.mean()*1e4:+.1f}bp (t {tstat(act_ret):+.2f}), "
         f"n={len(pol_ret)}, exited early "
         f"{np.mean(pol_ret != act_ret)*100:.0f}%")

    out = NOTES / f"wick_learn_{stamp()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def stage_entry(args) -> None:
    """The classifier as an ENTRY gate rather than an exit: delay entry to
    accept+60m, take only events the walk-forward model approves, and pay
    the price of the missed first-45-minutes drift. Three arms on the same
    OOS events (2019-26): enter at react (the flagship's timing), enter at
    +60m unconditionally (isolates the delay cost), enter at +60m only if
    P(continue) >= threshold."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    p = panel()
    q = load_minutes(p)
    side = q["side"].to_numpy()
    paths = build_paths(q, side)
    em = q["em"].to_numpy()
    move = q["move_pct"].to_numpy()
    run5 = q["run5d"].to_numpy()
    dv = q["dv"].to_numpy()
    yr = q["year"].to_numpy().astype(int)
    base, _ = run_rule(paths, r_hold())
    y = (base > 0).astype(int)

    feats, keep, px60 = [], [], []
    for i, pp in enumerate(paths):
        if pp is None or not np.isfinite(base[i]):
            continue
        f = features_at(pp, 60, False, em[i], move[i], run5[i], dv[i])
        if f is None:
            continue
        feats.append(f)
        keep.append(i)
        px60.append(pp.rc[min(pp.i_entry + 60, pp.n - 1)])
    X = np.nan_to_num(pd.DataFrame(feats).to_numpy(), nan=0.0)
    keep = np.array(keep)
    px60 = np.array(px60)                      # side-signed % at +60m
    yy = y[keep]
    yrs = yr[keep]
    prob = np.full(len(keep), np.nan)
    for t in range(2019, 2027):
        tr, te = yrs < t, yrs == t
        if te.sum() == 0 or tr.sum() < 200:
            continue
        gb = HistGradientBoostingClassifier(
            max_depth=3, max_iter=150, learning_rate=0.08,
            min_samples_leaf=40, random_state=7).fit(X[tr], yy[tr])
        prob[te] = gb.predict_proba(X[te])[:, 1]
    oos = np.isfinite(prob)

    r_react = base[keep]                        # net, entered at react
    t1 = np.array([paths[k].rc[paths[k].i_t1close] for k in keep])
    r_60 = (t1 - px60) / 100 - COSTS_BP / 1e4   # net, entered at +60m

    print(f"\n{int(oos.sum())} OOS events (2019-26), accept+60m entry gate\n")
    print(f"{'arm':46s}{'n':>6}{'bp':>8}{'t':>7}{'win%':>7}")
    print("-" * 74)

    def line(name, r):
        r = r[np.isfinite(r)]
        print(f"{name:46s}{len(r):>6}{r.mean()*1e4:>+8.1f}"
              f"{tstat(r):>+7.2f}{(r > 0).mean()*100:>7.1f}")

    line("enter at react (flagship timing)", r_react[oos])
    line("enter at +60m, all events (delay cost)", r_60[oos])
    for thr in (0.45, 0.55, 0.65):
        m = oos & (prob >= thr)
        line(f"enter at +60m only if P>={thr:.2f} "
             f"({m.sum()/oos.sum()*100:.0f}% taken)", r_60[m])
        line("  ...same events entered at react", r_react[m])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["anatomy", "rules", "fetch", "mrules",
                                      "learn", "entry"])
    ap.add_argument("--sides", default="tape", choices=["tape", "full"],
                    help="mrules only: tape = mechanical, full = the "
                         "paper's never-stand-down LLM policy")
    args = ap.parse_args()
    {"anatomy": stage_anatomy, "rules": stage_rules, "fetch": stage_fetch,
     "mrules": stage_mrules, "learn": stage_learn,
     "entry": stage_entry}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
