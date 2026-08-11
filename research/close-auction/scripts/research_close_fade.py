"""The closing-auction concession — new territory, the OPG trick's mirror.

gff's whole edge is an auction order nobody clicks (09:28 MOO). This study
asks whether the CLOSING auction pays the same kind of toll: when the late
tape moves hard into the bell (MOC-benchmarked flow, leveraged-ETF
rebalance, index adds), does the closing print dislocate in the flow's
direction — and does that concession revert by the next open? The taker
side of that trade is retail-placeable on this account: MOC/LOC against
the late move (TIF=cls, UNVERIFIED on Alpaca paper — a broker probe is a
gate before any build), exit next-day MOO. Literature prior:
Bogousslavsky-Muravyev measure closing-print deviations that revert
overnight; decay post-publication is exactly what this must check.

Design, pre-stated:
  universe   dv_prev >= $150M, c_prev >= $10 (gff's floors), top-40 by
             dv_prev per day, 2022-2026.
  signal     late-day move r_1530_1545 = px(15:45)/px(15:30) - 1, known
             before every MOC cutoff (15:50 NYSE / 15:55 Nasdaq).
             Condition family: |r| >= {30, 50, 75, 100}bp, reported in
             FULL. PRIMARY CELL, declared before results: |r| >= 50bp,
             FADE at the close print, exit next open, net@5bp.
  entries    the official close print (raw daily c — the auction).
  exits      next open (primary; MOO) and next close (secondary), P&L as
             ADJUSTED ratios (total-return basis: dividends accrue to the
             holder/against the short, splits safe).
  decompose  concession = close/px(15:59) - 1 signed by flow (is the
             auction itself dislocated) vs overnight reversion (next open
             vs close) — the trade is the sum, the mechanism is the split.
  costs      5bp/trade primary (two auction prints, no spread crossed),
             10bp stress.
  nulls      (a) unconditional overnight drift of the same top-40 books;
             (b) the same fade rule at the 15:59 tape instead of the
             auction print (does the auction print itself pay).
  basis rule the signal and concession use RAW prints only; P&L ratios
             use the ADJUSTED panel only. Levels never mix (scan-note §4).

Run:  python scripts/research_close_fade.py fetch    (resumable, ~1,130 days)
      python scripts/research_close_fade.py score
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
MARKS_F = CACHE / "close_marks.parquet"

START, END = "2022-01-03", "2026-08-04"
TOP_N = 40
DV_MIN, PX_MIN = 1.5e8, 10.0
THRESH_BP = (30.0, 50.0, 75.0, 100.0)
PRIMARY_THRESH = 50.0
COST_BP, STRESS_BP = 5.0, 10.0
CHUNK = 75


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def daily_panels() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(raw o/c, adjusted o/c) for the study window plus a year of lead."""
    def load(prefix: str, cols: list[str]) -> pd.DataFrame:
        frames = [pd.read_parquet(p, columns=cols)
                  for p in sorted(PEAD_CACHE.glob(f"{prefix}_*.parquet"))]
        df = pd.concat(frames, ignore_index=True)
        df["date"] = df["date"].astype(str)
        return (df[(df["date"] >= "2021-06-01") & (df["date"] <= END)]
                .drop_duplicates(subset=["symbol", "date"])
                .sort_values(["symbol", "date"]))
    raw = load("bars_rawoc", ["symbol", "date", "o", "c"])
    adj = load("bars_ohlc", ["symbol", "date", "o", "c", "v"])
    return raw, adj


def day_universe(adj: pd.DataFrame) -> pd.DataFrame:
    """Top-40 by prior-day dollar volume under the gff floors, per day."""
    g = adj.groupby("symbol", sort=False)
    adj = adj.assign(c_prev=g["c"].shift(1), v_prev=g["v"].shift(1))
    adj["dv_prev"] = adj["c_prev"] * adj["v_prev"]
    ok = ((adj["date"] >= START) & (adj["dv_prev"] >= DV_MIN)
          & (adj["c_prev"] >= PX_MIN))
    u = adj[ok].copy()
    u["rank"] = u.groupby("date")["dv_prev"].rank(ascending=False)
    return u[u["rank"] <= TOP_N][["symbol", "date"]]


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    CACHE.mkdir(parents=True, exist_ok=True)
    _, adj = daily_panels()
    uni = day_universe(adj)
    prior, have = pd.DataFrame(), set()
    if MARKS_F.exists():
        prior = pd.read_parquet(MARKS_F)
        have = set(prior["date"].astype(str))
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    days = [d for d in sorted(uni["date"].unique()) if d not in have]
    print(f"{len(days)} days to fetch ({len(uni)} symbol-days)")
    rows = []

    def flush():
        nonlocal rows, prior
        if not rows:
            return
        df = pd.DataFrame(rows)
        allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
        allf = allf.drop_duplicates(subset=["symbol", "date"])
        allf.to_parquet(MARKS_F)
        prior, rows = allf, []

    for n, day in enumerate(days):
        syms = sorted(uni.loc[uni["date"] == day, "symbol"])
        d = datetime.fromisoformat(day)
        marks: dict[str, dict] = {sym: {} for sym in syms}
        okday = True
        for i in range(0, len(syms), CHUNK):
            chunk = syms[i:i + CHUNK]
            try:
                res = api.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame.Minute,
                    start=d.replace(hour=15, minute=29, tzinfo=ET),
                    end=d.replace(hour=16, minute=0, tzinfo=ET),
                    feed="sip"))
            except Exception as exc:                   # noqa: BLE001
                print(f"  {day}: {str(exc)[:70]}")
                _time.sleep(1.0)
                okday = False
                break
            for sym in chunk:
                for b in res.data.get(sym) or []:
                    hm = b.timestamp.astimezone(ET).strftime("%H:%M")
                    if hm in ("15:29", "15:44", "15:59"):
                        marks[sym][hm] = float(b.close)
        if not okday:
            continue
        for sym in syms:
            m = marks[sym]
            if "15:29" not in m:
                continue
            rows.append({"symbol": sym, "date": day,
                         "px1530": m.get("15:29"),
                         "px1545": m.get("15:44"),
                         "px1559": m.get("15:59")})
        if n % 50 == 0:
            print(f"  {n}/{len(days)} ({day}, {len(rows)} rows)", flush=True)
            flush()
        _time.sleep(0.3)
    flush()
    print(f"cached {len(prior)} close-mark rows -> {MARKS_F}")


def stage_score(args) -> None:
    raw, adj = daily_panels()
    marks = pd.read_parquet(MARKS_F).dropna(subset=["px1530", "px1545"])
    raw = raw.sort_values(["symbol", "date"])
    g = raw.groupby("symbol", sort=False)
    raw = raw.assign(o_next_raw=g["o"].shift(-1))
    adj = adj.sort_values(["symbol", "date"])
    ga = adj.groupby("symbol", sort=False)
    adj = adj.assign(o_next=ga["o"].shift(-1), c_next=ga["c"].shift(-1))
    adj["ov_ret"] = adj["o_next"] / adj["c"] - 1          # total-return o/n
    adj["nc_ret"] = adj["c_next"] / adj["c"] - 1
    j = (marks
         .merge(raw[["symbol", "date", "c", "o_next_raw"]],
                on=["symbol", "date"], how="inner")
         .merge(adj[["symbol", "date", "ov_ret", "nc_ret"]],
                on=["symbol", "date"], how="inner"))
    j = j.dropna(subset=["c", "ov_ret"])
    j["late_bp"] = (j["px1545"] / j["px1530"] - 1) * 1e4
    j["conc_bp"] = np.where(j["px1559"].notna(),
                            (j["c"] / j["px1559"] - 1) * 1e4, np.nan)
    j["year"] = j["date"].str[:4]
    side = -np.sign(j["late_bp"])           # fade the late move
    j["fade_open_bp"] = side * j["ov_ret"] * 1e4
    j["fade_close_bp"] = side * j["nc_ret"] * 1e4
    j["fade_tape_bp"] = np.where(
        j["px1559"].notna(),
        side * ((1 + j["ov_ret"]) * j["c"] / j["px1559"] - 1) * 1e4, np.nan)
    j["conc_signed"] = np.sign(j["late_bp"]) * j["conc_bp"]

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# The closing-auction concession — {STAMP}")
    emit()
    emit(f"{len(j):,} symbol-days ({j['date'].nunique():,} days, top-{TOP_N} "
         f"liquid, {START[:4]}-{END[:4]}). Signal = 15:30->15:45 move "
         "(known before every MOC cutoff); fade at the closing print, exit "
         "next open (MOO), total-return basis. conc = close vs 15:59 tape, "
         "signed by the flow (positive = the auction overshoots the move).")
    emit()

    base_all = j["fade_open_bp"].mean()
    emit(f"Unconditional 'fade' baseline on all rows (null a): "
         f"{base_all:+.1f}bp gross — the conditioning must beat this.")
    emit()

    emit("## Threshold family (fade at close print, exit next open, gross "
         "and net@5)")
    emit()
    emit("| |late| >= | n | /day | conc bp (t) | fade->open bp | t | net@5 | net@10 |")
    emit("|---|---|---|---|---|---|---|---|")
    for thr in THRESH_BP:
        m = j["late_bp"].abs() >= thr
        x = j.loc[m, "fade_open_bp"].dropna()
        c = j.loc[m, "conc_signed"].dropna()
        mark = " **<-- primary**" if thr == PRIMARY_THRESH else ""
        emit(f"| {thr:.0f}bp | {len(x):,} "
             f"| {len(x) / j['date'].nunique():.1f} "
             f"| {c.mean():+.1f} ({tstat(c.to_numpy()):+.1f}) "
             f"| {x.mean():+.1f} | {tstat(x.to_numpy()):+.2f} "
             f"| {x.mean() - COST_BP:+.1f} "
             f"| {x.mean() - STRESS_BP:+.1f}{mark} |")
    emit()

    m = j["late_bp"].abs() >= PRIMARY_THRESH
    emit("## Primary cell, split")
    emit()
    emit("| slice | n | fade->open bp | t | net@5 |")
    emit("|---|---|---|---|---|")
    for label, mm in (
            ("late move UP -> SHORT at close", m & (j["late_bp"] > 0)),
            ("late move DOWN -> LONG at close", m & (j["late_bp"] < 0))):
        x = j.loc[mm, "fade_open_bp"].dropna()
        emit(f"| {label} | {len(x):,} | {x.mean():+.1f} "
             f"| {tstat(x.to_numpy()):+.2f} | {x.mean() - COST_BP:+.1f} |")
    for y in sorted(j["year"].unique()):
        x = j.loc[m & (j["year"] == y), "fade_open_bp"].dropna()
        emit(f"| {y} | {len(x):,} | {x.mean():+.1f} "
             f"| {tstat(x.to_numpy()):+.2f} | {x.mean() - COST_BP:+.1f} |")
    emit()

    emit("## Mechanism decomposition (primary cell)")
    emit()
    x_open = j.loc[m, "fade_open_bp"].dropna()
    x_tape = j.loc[m, "fade_tape_bp"].dropna()
    x_conc = j.loc[m, "conc_signed"].dropna()
    x_nc = j.loc[m, "fade_close_bp"].dropna()
    emit(f"- entered at the AUCTION print -> next open: {x_open.mean():+.1f}bp "
         f"(t {tstat(x_open.to_numpy()):+.2f}, n {len(x_open):,})")
    emit(f"- entered at the 15:59 TAPE -> next open (null b): "
         f"{x_tape.mean():+.1f}bp (t {tstat(x_tape.to_numpy()):+.2f}) — the "
         "difference is what the auction print itself pays")
    emit(f"- the concession alone (close vs 15:59, flow-signed): "
         f"{x_conc.mean():+.1f}bp (t {tstat(x_conc.to_numpy()):+.2f})")
    emit(f"- held to next CLOSE instead: {x_nc.mean():+.1f}bp "
         f"(t {tstat(x_nc.to_numpy()):+.2f}) — is the reversion done by "
         "the open")
    emit()
    emit("Anyone shopping the family table owes a x4 haircut; the primary "
         "cell was declared in the docstring. TIF=cls acceptance on the "
         "paper API is UNVERIFIED and gates any build.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_close_fade.py score` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"close_fade_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def stage_xray(args) -> None:
    """The pre-registration battery the score note queued: earnings x-ray
    (is the dumper-bounce just pre-AMC risk warehousing, or post-earnings
    drift?), the sub-split family (depth, day-of-week — reported in FULL),
    and the slot-level account sim (top-4/day by dump depth, 25% each)."""
    raw, adj = daily_panels()
    marks = pd.read_parquet(MARKS_F).dropna(subset=["px1530", "px1545"])
    adj = adj.sort_values(["symbol", "date"])
    ga = adj.groupby("symbol", sort=False)
    adj = adj.assign(o_next=ga["o"].shift(-1))
    adj["ov_ret"] = adj["o_next"] / adj["c"] - 1
    j = marks.merge(adj[["symbol", "date", "ov_ret"]],
                    on=["symbol", "date"], how="inner").dropna(
        subset=["ov_ret"])
    j["late_bp"] = (j["px1545"] / j["px1530"] - 1) * 1e4
    j["year"] = j["date"].str[:4]
    sig = j[j["late_bp"] <= -PRIMARY_THRESH].copy()      # the LONG side
    sig["bp"] = sig["ov_ret"] * 1e4

    ev = pd.concat([pd.read_parquet(f, columns=["symbol", "date", "acc_min"])
                    for f in sorted((ROOT / "research" / "pead-llm-gate"
                                     / "cache").glob("events_v2_*.parquet"))],
                   ignore_index=True)
    ev["date"] = ev["date"].astype(str)
    tonight = set(zip(ev.loc[ev["acc_min"] >= 930, "symbol"],
                      ev.loc[ev["acc_min"] >= 930, "date"]))
    ev["d_next"] = (pd.to_datetime(ev["date"])
                    + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    recent = set(zip(ev["symbol"], ev["d_next"])) | set(
        zip(ev.loc[ev["acc_min"] < 570, "symbol"],
            ev.loc[ev["acc_min"] < 570, "date"]))
    sig["reports_tonight"] = [
        (s, d) in tonight for s, d in zip(sig["symbol"], sig["date"])]
    sig["reported_recently"] = [
        (s, d) in recent for s, d in zip(sig["symbol"], sig["date"])]

    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    def row(label, mask):
        x = sig.loc[mask, "bp"].to_numpy()
        x = x[np.isfinite(x)]
        if len(x) < 20:
            emit(f"| {label} | {len(x)} | — | — | — |")
            return
        emit(f"| {label} | {len(x):,} | {x.mean():+.1f} "
             f"| {tstat(x):+.2f} | {x.mean() - COST_BP:+.1f} |")

    emit(f"# close_fade pre-registration battery — {STAMP}")
    emit()
    emit(f"{len(sig):,} long signals (late <= -{PRIMARY_THRESH:.0f}bp). "
         "Earnings flags from the events_v2 panel (8-K acceptances, "
         "2016-26): `reports_tonight` = same symbol accepted >= 15:30 "
         "that day; `reported_recently` = accepted the prior day or "
         "pre-open same day.")
    emit()
    emit("## Earnings x-ray (gross bp to next open)")
    emit()
    emit("| slice | n | bp | t | net@5 |")
    emit("|---|---|---|---|---|")
    row("ALL long signals", pd.Series(True, index=sig.index))
    row("reports TONIGHT (warehouses the print)", sig["reports_tonight"])
    row("reported recently (post-earnings drift)", sig["reported_recently"])
    clean = ~sig["reports_tonight"] & ~sig["reported_recently"]
    row("CLEAN (no earnings adjacency)", clean)
    emit()
    emit("## Sub-split family on the CLEAN set (report in full; x9 family)")
    emit()
    emit("| slice | n | bp | t | net@5 |")
    emit("|---|---|---|---|---|")
    d = sig["late_bp"].abs()
    for lab, m in (("depth 50-75bp", (d >= 50) & (d < 75)),
                   ("depth 75-100bp", (d >= 75) & (d < 100)),
                   ("depth 100-150bp", (d >= 100) & (d < 150)),
                   ("depth >= 150bp", d >= 150)):
        row(lab, clean & m)
    dow = pd.to_datetime(sig["date"]).dt.dayofweek
    for lab, m in (("Mon", dow == 0), ("Tue-Thu", (dow >= 1) & (dow <= 3)),
                   ("Fri (holds the weekend)", dow == 4)):
        row(lab, clean & m)
    for y in sorted(sig["year"].unique()):
        row(f"{y} clean", clean & (sig["year"] == y))
    emit()

    # ---- slot-level account sim ------------------------------------------
    emit("## Slot-level account sim (CLEAN set, top-4/day by depth, "
         "25%/slot, @5bp)")
    emit()
    spy = spy_ret = None
    spyf = sorted((ROOT / "research" / "pead-llm-gate" / "cache").glob(
        "bench_SPY_*.parquet"))
    spy = (pd.concat([pd.read_parquet(f) for f in spyf])
           .drop_duplicates("date").sort_values("date"))
    spy["date"] = spy["date"].astype(str)
    spy_ret = spy.set_index("date")["close"].pct_change()
    days = [d_ for d_ in spy_ret.index if START <= d_ <= max(sig["date"])]
    picks = (sig[clean].assign(depth=sig.loc[clean, "late_bp"].abs())
             .sort_values(["date", "depth"], ascending=[True, False])
             .groupby("date").head(4))
    net = picks["bp"] - COST_BP
    day_ret = (net * 0.25 / 1e4).groupby(picks["date"]).sum()
    ret = day_ret.reindex(days).fillna(0.0)
    eq = np.cumprod(1 + ret.to_numpy())
    years = len(days) / 252
    ann = float(eq[-1] ** (1 / years) - 1) * 100
    shp = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252))
    mdd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
    jj = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
    b = float(np.cov(jj.iloc[:, 0], jj.iloc[:, 1], ddof=1)[0, 1]
              / np.var(jj.iloc[:, 1], ddof=1))
    emit(f"- {len(picks):,} slot-trades on {picks['date'].nunique():,} "
         f"active days of {len(days):,}: ann {ann:+.2f}%, Sharpe "
         f"{shp:+.2f}, maxDD {mdd:.1f}%, beta {b:+.3f}")
    emit()
    emit("Overnight beta note: every slot holds the close->open gap; the "
         "beta above is the measured net-of-conditioning number, not an "
         "assumption.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_close_fade.py xray` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"close_fade_xray_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def stage_recut(args) -> None:
    """ONE pre-declared re-cut, stated before running (the xray note's
    condition for any further look): CLEAN of earnings adjacency, depth
    >= 100bp, Tue/Wed/Thu only, top-2 slots by depth at 25% each, net@5.
    No other configuration is run; a second cut would be shopping."""
    raw, adj = daily_panels()
    marks = pd.read_parquet(MARKS_F).dropna(subset=["px1530", "px1545"])
    adj = adj.sort_values(["symbol", "date"])
    ga = adj.groupby("symbol", sort=False)
    adj = adj.assign(o_next=ga["o"].shift(-1))
    adj["ov_ret"] = adj["o_next"] / adj["c"] - 1
    j = marks.merge(adj[["symbol", "date", "ov_ret"]],
                    on=["symbol", "date"], how="inner").dropna(
        subset=["ov_ret"])
    j["late_bp"] = (j["px1545"] / j["px1530"] - 1) * 1e4
    j["year"] = j["date"].str[:4]
    sig = j[j["late_bp"] <= -100.0].copy()
    sig["bp"] = sig["ov_ret"] * 1e4
    dow = pd.to_datetime(sig["date"]).dt.dayofweek
    sig = sig[(dow >= 1) & (dow <= 3)]

    ev = pd.concat([pd.read_parquet(f, columns=["symbol", "date", "acc_min"])
                    for f in sorted((ROOT / "research" / "pead-llm-gate"
                                     / "cache").glob("events_v2_*.parquet"))],
                   ignore_index=True)
    ev["date"] = ev["date"].astype(str)
    tonight = set(zip(ev.loc[ev["acc_min"] >= 930, "symbol"],
                      ev.loc[ev["acc_min"] >= 930, "date"]))
    ev["d_next"] = (pd.to_datetime(ev["date"])
                    + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    recent = set(zip(ev["symbol"], ev["d_next"])) | set(
        zip(ev.loc[ev["acc_min"] < 570, "symbol"],
            ev.loc[ev["acc_min"] < 570, "date"]))
    clean = [ (s, d) not in tonight and (s, d) not in recent
              for s, d in zip(sig["symbol"], sig["date"]) ]
    sig = sig[np.array(clean)]

    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    emit(f"# close_fade re-cut, the ONE declared config — {STAMP}")
    emit()
    emit(f"{len(sig):,} signals (clean, depth >= 100bp, Tue-Thu). "
         "Fade at the close print, exit next open, net@5. Declared in the "
         "stage docstring before running; no other cut was tried.")
    emit()
    net = sig["bp"] - COST_BP
    emit(f"- per trade: {net.mean():+.1f}bp net (t "
         f"{tstat(net.to_numpy()):+.2f}, n {len(net):,}, "
         f"{len(net) / sig['date'].nunique():.1f}/active day)")
    emit()
    emit("| year | n | net bp | t |")
    emit("|---|---|---|---|")
    for y in sorted(sig["year"].unique()):
        x = (sig.loc[sig["year"] == y, "bp"] - COST_BP).to_numpy()
        emit(f"| {y} | {len(x):,} | {x.mean():+.1f} | {tstat(x):+.2f} |")
    emit()
    spyf = sorted((ROOT / "research" / "pead-llm-gate" / "cache").glob(
        "bench_SPY_*.parquet"))
    spy = (pd.concat([pd.read_parquet(f) for f in spyf])
           .drop_duplicates("date").sort_values("date"))
    spy["date"] = spy["date"].astype(str)
    spy_ret = spy.set_index("date")["close"].pct_change()
    days = [d for d in spy_ret.index if START <= d <= max(j["date"])]
    picks = (sig.assign(depth=sig["late_bp"].abs())
             .sort_values(["date", "depth"], ascending=[True, False])
             .groupby("date").head(2))
    day_ret = ((picks["bp"] - COST_BP) * 0.25 / 1e4).groupby(
        picks["date"]).sum()
    ret = day_ret.reindex(days).fillna(0.0)
    eq = np.cumprod(1 + ret.to_numpy())
    years = len(days) / 252
    ann = float(eq[-1] ** (1 / years) - 1) * 100
    sh = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252))
    dd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
    emit(f"Slot sim (top-2 by depth, 25% each): {len(picks):,} trades, "
         f"ann {ann:+.2f}%, Sharpe {sh:+.2f}, maxDD {dd:.1f}%")
    emit()
    emit("Verdict rule, declared: this cut earns a pre-registration ONLY "
         "if Sharpe >= 1.0 here AND stays positive ex-2026; otherwise "
         "close_fade stays parked with no further cuts.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_close_fade.py recut` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"close_fade_recut_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score", "xray", "recut"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score,
     "xray": stage_xray, "recut": stage_recut}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
