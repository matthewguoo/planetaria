"""Macro-release drift: the mechanical intraday family that is not short-vol.

Two scheduled prints move index products at a known clock: CPI at 08:30 ET
and the FOMC statement at 14:00 ET. The literature says the pre-announcement
drift died (Lucca-Moench post-2015, confirmed in our null battery), but the
POST-announcement continuation is a different claim: the first half-hour's
direction after the print continuing into the close. That is event-driven,
direction-agnostic, minutes-scale, retail-executable — and uncorrelated
with selling volatility, which is what the fund's other mechanical sleeve
does all afternoon.

Design (both legs from the cached 30-minute SIP bars, which include
premarket):

  FOMC   signal = the 14:00-14:30 bar's open->close move on statement
         days; trade its sign from 14:30 to the close.
  CPI    signal = the 08:30-09:00 bar's open->close move on release days;
         trade (a) from 09:00 premarket to the close, (b) from the 09:30
         open to the close — the entry a cautious account would take.

Dates: FOMC statement days from the null battery's own list
(federalreserve.gov); CPI release dates from ALFRED's archival release
calendar (alfred.stlouisfed.org, rid=10), which is the St. Louis Fed's
record of when each print actually landed. Costs: these are two crossings
in SPY/QQQ, ~1-1.5bp round trip; the tables are gross.

Run:  python scripts/research_macro_drift.py
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BARS_CACHE = ROOT / "research" / "intraday-mft" / "cache"
ASTRO_SCRIPTS = ROOT / "research" / "astro-null" / "scripts"
sys.path.insert(0, str(ASTRO_SCRIPTS))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_astro_null import FOMC  # noqa: E402  (the battery's own list)

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def cpi_dates() -> list[str]:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "cpi_release_dates.txt"
    if not f.exists():
        import httpx
        r = httpx.get(
            "https://alfred.stlouisfed.org/release/downloaddates?rid=10&ff=txt",
            timeout=30, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        f.write_text(r.text, encoding="utf-8")
    dates = sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", f.read_text(encoding="utf-8"))))
    return [d for d in dates if d >= "2016-01-01"]


def bars(sym: str) -> pd.DataFrame:
    df = pd.read_parquet(BARS_CACHE / f"bars30_{sym}.parquet")
    df["d"] = df["ts"].dt.date.astype(str)
    df["hm"] = df["ts"].dt.strftime("%H:%M")
    return df


def day_grid(df: pd.DataFrame, hms: list[str]) -> pd.DataFrame:
    out = None
    for hm in hms:
        sub = (df[df["hm"] == hm].set_index("d")[["o", "c"]]
               .rename(columns={"o": f"o{hm.replace(':', '')}",
                                "c": f"c{hm.replace(':', '')}"}))
        out = sub if out is None else out.join(sub, how="outer")
    return out


def main() -> None:
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Macro-release drift, SPY/QQQ 30m bars — {STAMP}")
    emit()
    fomc_days = {d for d in FOMC if d <= datetime.now(ET).date().isoformat()}
    cpi = cpi_dates()
    emit(f"{len(fomc_days)} FOMC statement days (2016->), {len(cpi)} CPI "
         "release days (ALFRED archival calendar). Signal = the release "
         "bar's own open->close direction; gross of ~1-1.5bp round trips.")
    emit()

    for sym in ("SPY", "QQQ"):
        g = day_grid(bars(sym), ["08:30", "09:30", "14:00", "14:30", "15:30"])
        emit(f"## {sym}")
        emit()
        emit("| leg | window | n | bp/trade | t | win% | worst bp |")
        emit("|---|---|---|---|---|---|---|")

        def row(label, wname, sig, entry, exitp, mask):
            m = mask & np.isfinite(sig) & np.isfinite(entry) & np.isfinite(exitp)
            s = np.sign(sig[m])
            r = s * (exitp[m] / entry[m] - 1) * 1e4
            r = r[s != 0]
            if len(r) < 15:
                return
            emit(f"| {label} | {wname} | {len(r)} | {r.mean():+.1f} "
                 f"| {tstat(r):+.2f} | {(r > 0).mean() * 100:.0f} "
                 f"| {r.min():+.0f} |")

        # FOMC: signal bar 14:00-14:30, trade 14:30 -> close.
        fm = g.index.isin(fomc_days)
        sig = (g["c1400"] / g["o1400"] - 1).to_numpy()
        entry = g["c1400"].to_numpy()          # the 14:30 print
        exitp = g["c1530"].to_numpy()          # the close
        idx = pd.to_datetime(g.index)
        halves = {"all": np.ones(len(g), bool),
                  "2016-20": np.asarray(idx < "2021-01-01"),
                  "2021-26": np.asarray(idx >= "2021-01-01")}
        for wname, hmask in halves.items():
            row("FOMC 14:30->close w/ statement bar", wname, sig, entry,
                exitp, fm & hmask)
        # CPI: signal bar 08:30-09:00.
        cm = g.index.isin(cpi)
        sig_c = (g["c0830"] / g["o0830"] - 1).to_numpy()
        for wname, hmask in halves.items():
            row("CPI 09:00(pre)->close w/ print bar", wname, sig_c,
                g["c0830"].to_numpy(), exitp, cm & hmask)
        for wname, hmask in halves.items():
            row("CPI 09:30 open->close w/ print bar", wname, sig_c,
                g["o0930"].to_numpy(), exitp, cm & hmask)
        emit()

    emit("Read against the fund: this family is long-gamma-shaped (it wants "
         "the day to keep moving) where the fly is short it — the point of "
         "the pairing. Anything that clears here gets its own study before "
         "any build.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"macro_drift_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
