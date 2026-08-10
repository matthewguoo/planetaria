"""Account-level backtest of the REGISTERED gap_fail_fade config. No sweeps.

The pre-registration (docs/pre-registration-gap-fail-fade.md) fixes it:
|gap| >= 1.5%, price >= $10, late premarket (09:15->09:29 here; ->09:27
live) turned >= 20bp AGAINST the gap, enter AT the auction print (09:28
MOO), exit the 09:31 close (hard stop 09:31:30), <= 2 positions per leg at
25% of allocation each. The live instance runs the LONG leg only
(enable_short_leg=false); both-legs is what the registration ultimately
wants. Slot contention resolves by premarket volume — a rank the live
scanner can know at 09:27.

Costs: the registration charges 10bp and halts above a measured 20bp; the
table shows 6/10/15/20 so the halt boundary is visible.

Run: python scripts/research_gff_account.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

MIN_TURN_BP = 20.0
MIN_PRICE = 10.0
SLOTS_PER_LEG = 2
POSITION_FRAC = 0.25
COSTS_BP = (6.0, 10.0, 15.0, 20.0)
ALLOC = 10_000.0


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def spy_daily_ret() -> pd.Series:
    frames = [pd.read_parquet(f) for f in sorted(PEAD_CACHE.glob("bench_SPY_*.parquet"))]
    spy = (pd.concat(frames, ignore_index=True)
           .drop_duplicates("date").sort_values("date"))
    spy["date"] = spy["date"].astype(str)
    return spy.set_index("date")["close"].pct_change().rename("SPY")


def alpha_beta(ret: pd.Series, bench: pd.Series) -> tuple[float, float]:
    j = pd.concat([ret, bench], axis=1, join="inner").dropna()
    if len(j) < 60:
        return float("nan"), float("nan")
    y, x = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
    beta = float(np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1))
    alpha = float((y.mean() - beta * x.mean()) * 252 * 100)
    return alpha, beta


def main() -> None:
    paths = pd.read_parquet(CACHE / "open_paths.parquet").drop_duplicates(
        subset=["symbol", "date"])
    pm = pd.read_parquet(CACHE / "pm_marks.parquet").drop_duplicates(
        subset=["symbol", "date"])
    j = paths.merge(pm, on=["symbol", "date"], how="inner")
    j = j.dropna(subset=["pm0915", "pm0929"])
    j = j[j["open"] >= MIN_PRICE]

    C1 = np.array([np.asarray(v, float)[1] for v in j["c"]])  # 09:31 close
    opens = j["open"].to_numpy()
    gap = j["gap"].to_numpy()
    pm_trend = (j["pm0929"] / j["pm0915"] - 1).to_numpy() * 1e4
    fading = (np.abs(pm_trend) >= MIN_TURN_BP) & \
             (np.sign(pm_trend) != np.sign(gap * 1e4)) & \
             np.isfinite(C1) & (opens > 0)

    # Fade the gap: side = -sign(gap). Per-trade gross bp at the auction.
    ret_bp = -np.sign(gap) * (C1 / opens - 1) * 1e4
    df = pd.DataFrame({
        "date": j["date"].to_numpy(), "symbol": j["symbol"].to_numpy(),
        "bp": ret_bp, "long": gap < 0, "pmvol": j["pmvol"].to_numpy(),
    })[fading].dropna(subset=["bp"])

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# gap_fail_fade, REGISTERED config, account level — {STAMP}")
    emit()
    emit(f"{len(df):,} qualifying fades on {df['date'].nunique():,} days, "
         f"2022-01..2026-08 (top-8-liquid |gap|>=1.5% universe, px>=$10, "
         f"PM turn >= {MIN_TURN_BP:.0f}bp against). Slots: "
         f"{SLOTS_PER_LEG}/leg by premarket volume, {POSITION_FRAC:.0%} of "
         "allocation each; entry at the auction print, exit the 09:31 "
         "close. Flat days stay in the daily series.")
    emit()

    spy = spy_daily_ret()
    all_days = sorted(set(df["date"]) | set(spy.index[spy.index >= "2022-01-01"]))
    all_days = [d for d in all_days if d <= max(df["date"])]

    daily_out: dict[str, pd.Series] = {}
    emit("| legs | cost bp | trades | net bp/trade | t | win% | trades/day "
         "| ann ret % | Sharpe | maxDD % | alpha vs SPY %/yr | beta |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for legs_name, mask in (("LONG only (live today)", df["long"]),
                            ("both legs", pd.Series(True, index=df.index))):
        sel = (df[mask]
               .sort_values(["date", "pmvol"], ascending=[True, False])
               .groupby(["date", "long"]).head(SLOTS_PER_LEG))
        for cost in COSTS_BP:
            net = sel["bp"] - cost
            day_ret = (net * POSITION_FRAC / 1e4).groupby(sel["date"]).sum()
            ret = day_ret.reindex(all_days).fillna(0.0)
            dollars = ret * ALLOC
            curve = ALLOC + dollars.cumsum()
            peak = curve.cummax()
            mdd = float(((peak - curve) / peak).max() * 100)
            years = len(all_days) / 252
            ann = float((curve.iloc[-1] / ALLOC) ** (1 / years) - 1) * 100
            sharpe = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252)) if ret.std(ddof=1) > 0 else 0.0
            a, b = alpha_beta(ret, spy)
            emit(f"| {legs_name} | {cost:.0f} | {len(sel):,} "
                 f"| {net.mean():+.1f} | {tstat(net.to_numpy()):+.2f} "
                 f"| {(net > 0).mean() * 100:.0f} | {len(sel) / len(all_days):.2f} "
                 f"| {ann:+.2f} | {sharpe:+.2f} | {mdd:.1f} "
                 f"| {a:+.2f} | {b:+.3f} |")
            if cost == 10.0:
                daily_out[legs_name] = ret
    emit()

    emit("## By year — 10bp cost")
    emit()
    emit("| year | LONG net bp/tr | t | n | BOTH net bp/tr | t | n |")
    emit("|---|---|---|---|---|---|---|")
    sel_l = (df[df["long"]].sort_values(["date", "pmvol"], ascending=[True, False])
             .groupby("date").head(SLOTS_PER_LEG))
    sel_b = (df.sort_values(["date", "pmvol"], ascending=[True, False])
             .groupby(["date", "long"]).head(SLOTS_PER_LEG))
    for y in sorted(set(d[:4] for d in df["date"])):
        L = sel_l[sel_l["date"].str.startswith(y)]["bp"] - 10.0
        B = sel_b[sel_b["date"].str.startswith(y)]["bp"] - 10.0
        emit(f"| {y} | {L.mean():+.1f} | {tstat(L.to_numpy()):+.2f} | {len(L)} "
             f"| {B.mean():+.1f} | {tstat(B.to_numpy()):+.2f} | {len(B)} |")
    emit()
    emit("Selection honesty: the fading cell is the SURVIVOR of the "
         "premarket split (its sibling 'aligned' cell died), so the "
         "per-trade edge carries surviving-cell risk; the by-year split and "
         "the slot-capped account view are the honesty checks. The 09:29 "
         "turn here vs 09:27 live is an accepted pre-reg deviation.")

    out_ret = daily_out["LONG only (live today)"]
    pd.DataFrame({"d": out_ret.index, "ret": out_ret.to_numpy()}).to_parquet(
        CACHE / "account_daily_gff_long.parquet")
    out_ret_b = daily_out["both legs"]
    pd.DataFrame({"d": out_ret_b.index, "ret": out_ret_b.to_numpy()}).to_parquet(
        CACHE / "account_daily_gff_both.parquet")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_gff_account.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gff_account_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
