"""Where between 15:30 (zero) and intrinsic (all of it) does the fly's edge
accrue — the exit study the 2026-08-10 handoff queued (#2).

The account study measured the registered config's endpoints: a 15:30
close-out earns nothing (+0.03bp/day) and holding to expiry intrinsic earns
everything (+2.01bp/day, Sharpe 1.26). The REGISTERED exit is 15:50 — never
measured, sitting somewhere on that curve. fly-1 is live at one set. This
study prices the curve at minute resolution so the exit is a measured
choice instead of a guessed one.

Design, pre-stated:
  structure  the registered fly: QQQ 0DTE, 14:00 entry marks from the
             existing straddle/wing caches (strike = 10:00 underlying
             rounded, 1.0% wings), guard credit/width in [0.10, 0.90]
             (Amendment 2's band), entry credit net of the $0.02 giveup.
  exits      buy the structure back at its leg prints at 15:30 / 15:35 /
             15:40 / 15:45 / 15:50 (registered) / 15:55 / 15:59, or settle
             at expiry intrinsic. Leg price at an exit = last print at or
             before the minute (>= 15:25). Short ATM legs REQUIRE a print
             within 10 minutes (else the day drops from that cell,
             counted); long wings with no print are valued 0 — selling
             the wing back at zero is the conservative side for us.
  friction   primary: 2c giveup at entry (in the credit) + 2c to cross on
             an early exit; intrinsic pays entry giveup only (assignment /
             pin friction unmodeled, stated). Stress row: 4c + 4c.
  basis      bp of the 10:00 underlying (px1000), matching every fly table.
  honesty    the 15:30 and intrinsic cells must reproduce the account
             study's shape before the in-between cells are believed; the
             curve is also reported on the common-core days where every
             exit is priceable. Pin-risk framing: |close - K| frequencies.

Run:  python scripts/research_fly_exit.py fetch     (~640 sessions, resumable)
      python scripts/research_fly_exit.py score
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
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_0dte_straddle import occ, tstat  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
PATHS_F = CACHE / "fly_exit_paths.parquet"

WING_SYM = "QQQ"
WP = 1.0
GUARD_LO, GUARD_HI = 0.10, 0.90
ENTRY_GIVEUP = 0.02
EXIT_CROSS = 0.02
STRESS_ENTRY, STRESS_CROSS = 0.04, 0.04
EXITS = ("15:30", "15:35", "15:40", "15:45", "15:50", "15:55", "15:59")
N_MIN = 40                       # path slots 15:25 .. 16:04
BASE_MIN = 15 * 60 + 25
ATM_MAX_STALE_MIN = 10


def _minute_idx(hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return h * 60 + m - BASE_MIN


def _joined_entry() -> pd.DataFrame:
    atm = pd.read_parquet(CACHE / "straddle_marks.parquet")
    atm = atm[atm["sym"] == WING_SYM].set_index("d")
    wings = pd.read_parquet(CACHE / "wing_marks.parquet").set_index("d")
    j = atm.join(wings, how="inner", rsuffix="_w")
    cols = ["C_1400", "P_1400", f"C{WP}_1400", f"P{WP}_1400"]
    j = j.dropna(subset=cols).copy()
    j["w"] = j[f"K_C_{WP}"] - j["k"]
    j["credit"] = (j["C_1400"] + j["P_1400"] - j[f"C{WP}_1400"]
                   - j[f"P{WP}_1400"] - ENTRY_GIVEUP)
    j["frac"] = j["credit"] / j["w"]
    return j


def stage_fetch(args) -> None:
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    s = get_settings()
    api = OptionHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    j = _joined_entry()
    prior, have = pd.DataFrame(), set()
    if PATHS_F.exists():
        prior = pd.read_parquet(PATHS_F)
        have = set(prior["d"].astype(str))
    today = datetime.now(ET).strftime("%Y-%m-%d")
    todo = [r for r in j.itertuples()
            if str(r.Index) not in have and str(r.Index) < today]
    print(f"{len(todo)} sessions to fetch (of {len(j)} with 14:00 entries)")
    rows = []

    def flush():
        nonlocal rows, prior
        if not rows:
            return
        df = pd.DataFrame(rows)
        allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
        allf = allf.drop_duplicates(subset=["d"])
        allf.to_parquet(PATHS_F)
        prior, rows = allf, []

    for n, r in enumerate(todo):
        d = datetime.strptime(str(r.Index), "%Y-%m-%d").date()
        legs = {"C0": occ(WING_SYM, d, "C", r.k),
                "P0": occ(WING_SYM, d, "P", r.k),
                "CW": occ(WING_SYM, d, "C", r.k + r.w),
                "PW": occ(WING_SYM, d, "P", r.k - r.w)}
        try:
            req = OptionBarsRequest(
                symbol_or_symbols=list(legs.values()),
                timeframe=TimeFrame.Minute,
                start=datetime(d.year, d.month, d.day, 15, 25, tzinfo=ET),
                end=datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET))
            data = api.get_option_bars(req).data
        except Exception as exc:                   # noqa: BLE001
            print(f"  {r.Index}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        rec = {"d": str(r.Index)}
        for name, s_ in legs.items():
            path = np.full(N_MIN, np.nan)
            for b in data.get(s_) or []:
                ts = b.timestamp.astimezone(ET)
                i = ts.hour * 60 + ts.minute - BASE_MIN
                if 0 <= i < N_MIN:
                    path[i] = float(b.close)
            rec[name] = path.tolist()
        rows.append(rec)
        if n % 50 == 0:
            print(f"  {n}/{len(todo)} ({r.Index})", flush=True)
            flush()
        _time.sleep(0.25)
    flush()
    print(f"cached {len(prior)} exit-path days -> {PATHS_F}")


def _leg_at(path: np.ndarray, hm: str, *, required_within: int | None) -> float:
    """Last print at or before the exit minute. required_within: max
    staleness in minutes for a valid mark (None = wing rule, missing -> 0)."""
    i = _minute_idx(hm)
    seg = path[:i + 1]
    ok = np.where(np.isfinite(seg))[0]
    if len(ok) == 0:
        return np.nan if required_within is not None else 0.0
    last = ok[-1]
    if required_within is not None and (i - last) > required_within:
        return np.nan
    return float(seg[last])


def stage_score(args) -> None:
    j = _joined_entry()
    paths = pd.read_parquet(PATHS_F).set_index("d")
    guard = (j["frac"] >= GUARD_LO) & (j["frac"] <= GUARD_HI)
    j = j[guard].join(paths, how="inner")
    P = {name: np.stack([np.asarray(v, float) for v in j[name]])
         for name in ("C0", "P0", "CW", "PW")}
    n_days = len(j)
    intr = ((j["close"] - j["k"]).abs()
            - np.maximum(0.0, j["close"] - (j["k"] + j["w"]))
            - np.maximum(0.0, (j["k"] - j["w"]) - j["close"]))
    px = j["px1000"].to_numpy()
    credit = j["credit"].to_numpy()
    yr = j.index.astype(str).str[:4].to_numpy()

    buyback = {}
    for hm in EXITS:
        c0 = np.array([_leg_at(P["C0"][i], hm, required_within=ATM_MAX_STALE_MIN)
                       for i in range(n_days)])
        p0 = np.array([_leg_at(P["P0"][i], hm, required_within=ATM_MAX_STALE_MIN)
                       for i in range(n_days)])
        cw = np.array([_leg_at(P["CW"][i], hm, required_within=None)
                       for i in range(n_days)])
        pw = np.array([_leg_at(P["PW"][i], hm, required_within=None)
                       for i in range(n_days)])
        buyback[hm] = c0 + p0 - cw - pw

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# The fly's exit curve, 15:30 -> settlement — {STAMP}")
    emit()
    emit(f"{n_days} guarded sessions (credit/width in [{GUARD_LO:.2f}, "
         f"{GUARD_HI:.2f}]), registered 14:00/1.0% structure, entry credit "
         f"net of {ENTRY_GIVEUP * 100:.0f}c. Early exits buy the structure "
         f"back at leg prints (ATM legs need a print within "
         f"{ATM_MAX_STALE_MIN}min; unprinted wings sell back at ZERO — "
         f"conservative) and pay {EXIT_CROSS * 100:.0f}c to cross; intrinsic "
         "pays entry giveup only (assignment/pin friction unmodeled). "
         "bp of the 10:00 underlying, 1 set.")
    emit()

    # ---- the curve --------------------------------------------------------
    emit("## The exit curve (all priceable days per cell)")
    emit()
    emit("| exit | n | mean bp/d | t | win% | worst bp | p5 bp | ann Sharpe |")
    emit("|---|---|---|---|---|---|---|---|")
    rows_curve = {}
    for hm in EXITS:
        pnl = (credit - buyback[hm] - EXIT_CROSS) / px * 1e4
        ok = np.isfinite(pnl)
        x = pnl[ok]
        rows_curve[hm] = pnl
        sh = x.mean() / x.std(ddof=1) * np.sqrt(252)
        emit(f"| {hm} | {ok.sum()} | {x.mean():+.2f} | {tstat(x):+.2f} "
             f"| {(x > 0).mean() * 100:.0f} | {x.min():+.0f} "
             f"| {np.percentile(x, 5):+.0f} | {sh:+.2f} |")
    pnl_i = (credit - intr.to_numpy()) / px * 1e4
    x = pnl_i[np.isfinite(pnl_i)]
    rows_curve["intrinsic"] = pnl_i
    emit(f"| intrinsic | {np.isfinite(pnl_i).sum()} | {x.mean():+.2f} "
         f"| {tstat(x):+.2f} | {(x > 0).mean() * 100:.0f} | {x.min():+.0f} "
         f"| {np.percentile(x, 5):+.0f} "
         f"| {x.mean() / x.std(ddof=1) * np.sqrt(252):+.2f} |")
    emit()

    # ---- common core ------------------------------------------------------
    core = np.ones(n_days, bool)
    for hm in EXITS:
        core &= np.isfinite(rows_curve[hm])
    core &= np.isfinite(pnl_i)
    emit(f"## Common-core curve ({int(core.sum())} days where every exit is "
         "priceable)")
    emit()
    emit("| exit | mean bp/d | t | delta vs intrinsic | share of intrinsic % |")
    emit("|---|---|---|---|---|")
    base_core = pnl_i[core].mean()
    for hm in list(EXITS) + ["intrinsic"]:
        x = rows_curve[hm][core]
        share = x.mean() / base_core * 100 if base_core != 0 else np.nan
        emit(f"| {hm} | {x.mean():+.2f} | {tstat(x):+.2f} "
             f"| {x.mean() - base_core:+.2f} | {share:.0f} |")
    emit()

    # ---- where the decay accrues -----------------------------------------
    emit("## Decay accrual by bucket (common core, mean bp of S captured "
         "per interval)")
    emit()
    emit("| interval | mean bp | t |")
    emit("|---|---|---|")
    seq = list(EXITS) + ["intrinsic"]
    for a, b in zip(seq[:-1], seq[1:]):
        d = rows_curve[b][core] - rows_curve[a][core]
        emit(f"| {a} -> {b} | {d.mean():+.2f} | {tstat(d):+.2f} |")
    emit()

    # ---- registered vs candidates, by year --------------------------------
    emit("## By year, net bp/day (candidate exits)")
    emit()
    emit("| year | n | 15:50 (registered) | 15:55 | 15:59 | intrinsic |")
    emit("|---|---|---|---|---|---|")
    for y in sorted(set(yr)):
        m = yr == y
        cells = []
        for hm in ("15:50", "15:55", "15:59", "intrinsic"):
            x = rows_curve[hm][m]
            x = x[np.isfinite(x)]
            cells.append(f"{x.mean():+.2f} (t {tstat(x):+.1f})")
        emit(f"| {y} | {int(m.sum())} | " + " | ".join(cells) + " |")
    emit()

    # ---- stress friction --------------------------------------------------
    emit("## Stress friction (entry 4c in place of 2c, exits cross 4c)")
    emit()
    emit("| exit | mean bp/d | t |")
    emit("|---|---|---|")
    for hm in ("15:50", "15:55", "15:59"):
        pnl = (credit - (STRESS_ENTRY - ENTRY_GIVEUP) - buyback[hm]
               - STRESS_CROSS) / px * 1e4
        x = pnl[np.isfinite(pnl)]
        emit(f"| {hm} | {x.mean():+.2f} | {tstat(x):+.2f} |")
    pnl = (credit - (STRESS_ENTRY - ENTRY_GIVEUP) - intr.to_numpy()) / px * 1e4
    x = pnl[np.isfinite(pnl)]
    emit(f"| intrinsic | {x.mean():+.2f} | {tstat(x):+.2f} |")
    emit()

    # ---- pin risk ---------------------------------------------------------
    dist = (j["close"] - j["k"]).abs()
    emit("## Pin framing (how often settlement lands near the short strike)")
    emit()
    for thr in (0.25, 0.50, 1.00):
        emit(f"- |close - K| <= ${thr:.2f}: {(dist <= thr).mean() * 100:.1f}% "
             f"of days")
    emit(f"- median |close - K|: ${dist.median():.2f}; wings are "
         f"${j['w'].median():.0f} away")
    emit()
    emit("A held-to-settlement fly leaves the short legs to expiry "
         "mechanics: the broker desk force-closes positions the account "
         "cannot exercise (observed 2026-07-29, market orders 15:30-15:58), "
         "so 'intrinsic' is not an executable policy on this account — "
         "15:59 is the latest self-managed exit the engine can own.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_fly_exit.py score` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"fly_exit_curve_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
