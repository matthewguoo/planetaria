"""Session astrology — the intraday clock canon, counted as a battery.

Matthew's list: London open/close effects, the NY-open-15-minutes canon,
the 10am reversal, lunch, power hour. The RTH-visible claims are all
testable on the mtape cache; this runs them as ONE battery with the
astro/calendar study's discipline: every cell reported, Bonferroni
counted across the family, nothing promoted without surviving it.
(True London-session sweep claims need premarket bars — a follow-up
fetch ONLY if something here survives.)

Cells, pre-stated (side-signed bp, net never shown — these are
CONDITIONING tests, not strategies; the question is whether the clock
carries information at all):
  1  first-15m direction -> 09:45-close continuation (the ORB creed)
  2  first-15m range quartile (narrow vs wide) -> |rest-of-day move|
  3  the 10:00 reversal: 09:30-10:00 move vs 10:00-11:00 move corr/sign
  4  London close turn: 10:00-11:00 move vs 11:00-11:30 move
  5  lunch drift: 12:00-13:30 unconditional + |move| vs other 90m blocks
  6  14:00 'algo hour': 13:30-14:00 vs 14:00-15:00 continuation
  7  power hour: day-so-far (09:30-15:00) direction -> 15:00-15:55
  8  SPY and QQQ singled out for cells 1, 3, 4, 7 (index-specific canon)
Family size ~16; Bonferroni at 0.05/16 -> |t| >= ~2.96 to survive.

Run: python scripts/research_session_astro.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
MTAPE = ROOT / "research" / "minute-tape" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

MARKS = {"0930": 0, "0945": 15, "1000": 30, "1100": 90, "1130": 120,
         "1200": 150, "1330": 240, "1400": 270, "1500": 330, "1555": 385}
N_TESTS = 16


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def build_marks() -> pd.DataFrame:
    rows = []
    for f in sorted(MTAPE.glob("mtape_*.parquet")):
        m = pd.read_parquet(f, columns=["symbol", "date", "minute", "c", "h", "l"])
        m = m[(m["minute"] >= 0) & (m["minute"] < 390)]
        for (sym, day), d in m.groupby(["symbol", "date"], sort=False):
            d = d.sort_values("minute")
            a = np.full(390, np.nan)
            a[d["minute"].to_numpy()] = d["c"].to_numpy(dtype=float)
            a = pd.Series(a).ffill().to_numpy()
            hi = np.full(390, np.nan)
            hi[d["minute"].to_numpy()] = d["h"].to_numpy(dtype=float)
            lo = np.full(390, np.nan)
            lo[d["minute"].to_numpy()] = d["l"].to_numpy(dtype=float)
            if not np.isfinite(a[MARKS["0930"]]) or not np.isfinite(a[385]):
                continue
            r = {"symbol": sym, "date": day}
            for k, i in MARKS.items():
                r[f"c{k}"] = a[i]
            r["r15_hi"] = np.nanmax(hi[:16])
            r["r15_lo"] = np.nanmin(lo[:16])
            rows.append(r)
        print(f"  {f.name}: {len(rows):,} rows", flush=True)
    return pd.DataFrame(rows)


def main() -> None:
    cache_f = CACHE / "session_marks.parquet"
    if cache_f.exists():
        df = pd.read_parquet(cache_f)
    else:
        df = build_marks()
        df.to_parquet(cache_f)
    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    def seg(a, b):
        return (df[f"c{b}"] / df[f"c{a}"] - 1) * 1e4

    emit(f"# Session astrology, the battery — {STAMP}")
    emit()
    emit(f"{len(df):,} symbol-days (top-100 liquid, 2022-26). All cells "
         f"reported; family of ~{N_TESTS}; Bonferroni survival needs "
         "|t| >= ~2.96. Conditioning tests, gross, no strategy claimed.")
    emit()
    emit("| # | claim | n | bp (signed) | t | survives |")
    emit("|---|---|---|---|---|---|")
    k = 0

    def cell(label, x):
        nonlocal k
        k += 1
        x = x[np.isfinite(x)]
        t = tstat(x)
        emit(f"| {k} | {label} | {len(x):,} | {x.mean():+.2f} "
             f"| {t:+.2f} | {'YES' if abs(t) >= 2.96 else 'no'} |")

    r_open = seg("0930", "0945")
    r_rest = seg("0945", "1555")
    cell("first-15m direction -> rest of day (continuation)",
         (np.sign(r_open) * r_rest).to_numpy())
    rng15 = ((df["r15_hi"] - df["r15_lo"]) / df["c0930"] * 1e4)
    q1, q3 = rng15.quantile(0.25), rng15.quantile(0.75)
    wide = rng15 >= q3
    narrow = rng15 <= q1
    absrest = np.abs(r_rest)
    cell("wide-open-range days: |rest| minus narrow days' |rest|",
         (absrest[wide].to_numpy() - float(np.nanmean(absrest[narrow])))
         if wide.sum() else np.array([]))
    m1 = seg("0930", "1000")
    m2 = seg("1000", "1100")
    cell("the 10:00 reversal (sign(0930-1000) x 1000-1100)",
         (np.sign(m1) * m2).to_numpy())
    m3 = seg("1100", "1130")
    cell("London close turn (sign(1000-1100) x 1100-1130)",
         (np.sign(m2) * m3).to_numpy())
    lunch = seg("1200", "1330")
    cell("lunch drift 12:00-13:30 (unconditional)", lunch.to_numpy())
    pre14 = seg("1330", "1400")
    post14 = seg("1400", "1500")
    cell("14:00 continuation (sign(1330-1400) x 1400-1500)",
         (np.sign(pre14) * post14).to_numpy())
    dsf = seg("0930", "1500")
    ph = seg("1500", "1555")
    cell("power hour continuation (sign(day-so-far) x 15:00-15:55)",
         (np.sign(dsf) * ph).to_numpy())
    for sym in ("SPY", "QQQ"):
        s = df["symbol"] == sym
        if s.sum() < 200:
            continue
        cell(f"{sym}: first-15m -> rest",
             (np.sign(r_open[s]) * r_rest[s]).to_numpy())
        cell(f"{sym}: 10:00 reversal",
             (np.sign(m1[s]) * m2[s]).to_numpy())
        cell(f"{sym}: London close turn",
             (np.sign(m2[s]) * m3[s]).to_numpy())
        cell(f"{sym}: power hour continuation",
             (np.sign(dsf[s]) * ph[s]).to_numpy())
    emit()
    emit(f"{k} cells run. Anything surviving Bonferroni gets ONE follow-up "
         "(year split) before any further attention; the premarket "
         "London-sweep fetch happens only on a survivor.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_session_astro.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"session_astro_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
