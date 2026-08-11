"""The canon rescue battery — ML filtering over the dead session cells.

Matthew's order: dead canon doesn't close until a gate has tried to find
the conditional subset where it pays (the gff-gate paradigm: ungated
+12.3 -> gated +21.1; the confirmed-entry gate: +41 -> +80). This runs
that rescue attempt on the four session-clock families the battery
killed, each a ~114k-trade pool — big, honest, walk-forward.

Families (trade defined exactly as the creed preaches; one config each):
  F1 first-15m continuation: enter 09:45 with sign(0930->0945), exit 15:55
  F2 the 10:00 reversal: enter 10:00 against sign(0930->1000), exit 11:00
  F3 14:00 continuation: enter 14:00 with sign(1330->1400), exit 15:00
  F4 power hour: enter 15:00 with sign(0930->1500), exit 15:55

Gate: HGB walk-forward (train < test year, test 2024/25/26) on
decision-time features only (segments so far, first-15 range, overnight
gap, prior-day return, 20d vol, dollar-volume rank, day-of-week).
Readout per family: ungated vs gated (P >= 0.55 — one threshold,
declared, no tau sweep) net@10 per trade + year split + one train-label
placebo.

RESCUE BAR, declared before any result: gated net@10 >= +5bp/trade AND
>= 2 of 3 OOS years positive net AND placebo shows no comparable lift.
Anything failing stays dead — a gate that cannot clear five basis
points is a mirage with extra steps.

Run: python scripts/research_canon_rescue.py
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
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

COST_RT_BP = 10.0
TAU = 0.55
SEED = 20260811


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def load_panel() -> pd.DataFrame:
    df = pd.read_parquet(CACHE / "session_marks.parquet")
    daily = pd.concat(
        [pd.read_parquet(p, columns=["symbol", "date", "c", "v"])
         for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))],
        ignore_index=True)
    daily["date"] = daily["date"].astype(str)
    daily = (daily[daily["date"] >= "2021-06-01"]
             .drop_duplicates(["symbol", "date"])
             .sort_values(["symbol", "date"]))
    g = daily.groupby("symbol", sort=False)
    ret = daily["c"] / g["c"].shift(1) - 1
    daily["c_prev"] = g["c"].shift(1)
    daily["ret1"] = ret.groupby(daily["symbol"]).shift(1)
    daily["rv20"] = (ret.groupby(daily["symbol"])
                     .rolling(20, min_periods=15).std()
                     .reset_index(level=0, drop=True)
                     .groupby(daily["symbol"]).shift(1)) * np.sqrt(252)
    daily["dv_prev"] = (daily["c"] * daily["v"]).groupby(
        daily["symbol"]).shift(1)
    df = df.merge(daily[["symbol", "date", "c_prev", "ret1", "rv20",
                         "dv_prev"]], on=["symbol", "date"], how="left")
    df["gap_bp"] = (df["c0930"] / df["c_prev"] - 1) * 1e4
    df["first15_bp"] = (df["c0945"] / df["c0930"] - 1) * 1e4
    df["range15_bp"] = (df["r15_hi"] - df["r15_lo"]) / df["c0930"] * 1e4
    df["dv_log"] = np.log1p(df["dv_prev"])
    df["dow"] = pd.to_datetime(df["date"]).dt.dayofweek.astype(float)
    df["year"] = df["date"].str[:4].astype(int)
    return df


def seg(df, a, b):
    return (df[f"c{b}"] / df[f"c{a}"] - 1) * 1e4


def main() -> None:
    rng = np.random.default_rng(SEED)
    df = load_panel()
    base_feats = ["gap_bp", "first15_bp", "range15_bp", "ret1", "rv20",
                  "dv_log", "dow"]

    fams = {
        "F1 first-15m continuation (09:45->15:55)": {
            "sig": np.sign(seg(df, "0930", "0945")),
            "ret": seg(df, "0945", "1555"),
            "extra": [],
        },
        "F2 the 10:00 reversal (10:00->11:00)": {
            "sig": -np.sign(seg(df, "0930", "1000")),
            "ret": seg(df, "1000", "1100"),
            "extra": {"m0930_1000": seg(df, "0930", "1000")},
        },
        "F3 14:00 continuation (14:00->15:00)": {
            "sig": np.sign(seg(df, "1330", "1400")),
            "ret": seg(df, "1400", "1500"),
            "extra": {"m0930_1330": seg(df, "0930", "1330"),
                      "m1330_1400": seg(df, "1330", "1400")},
        },
        "F4 power hour (15:00->15:55)": {
            "sig": np.sign(seg(df, "0930", "1500")),
            "ret": seg(df, "1500", "1555"),
            "extra": {"m0930_1500": seg(df, "0930", "1500"),
                      "m1400_1500": seg(df, "1400", "1500")},
        },
    }

    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    emit(f"# The canon rescue battery — {STAMP}")
    emit()
    emit(f"{len(df):,} symbol-days. Gate HGB walk-forward 2024-26, single "
         f"tau {TAU} (declared), net@{COST_RT_BP:.0f}bp RT. Rescue bar in "
         "the docstring; anything failing stays dead.")
    emit()
    emit("| family | book | n | net bp/tr | t | 2024 | 2025 | 2026 | AUC "
         "| verdict |")
    emit("|---|---|---|---|---|---|---|---|---|---|")

    for name, fam in fams.items():
        sig = fam["sig"].to_numpy() if hasattr(fam["sig"], "to_numpy") \
            else fam["sig"]
        r = (sig * fam["ret"]).to_numpy() if hasattr(fam["ret"], "to_numpy") \
            else sig * fam["ret"]
        feats = pd.DataFrame({c: df[c] for c in base_feats})
        if fam["extra"]:
            for k, v in fam["extra"].items():
                feats[k] = v
        feats["sig"] = sig
        ok = np.isfinite(r) & (sig != 0)
        X = np.nan_to_num(feats.to_numpy(dtype=float))[ok]
        rr = r[ok]
        yrs = df["year"].to_numpy()[ok]
        y = (rr > 0).astype(int)

        prob = np.full(len(rr), np.nan)
        prob_pl = np.full(len(rr), np.nan)
        for t in (2024, 2025, 2026):
            tr, te = yrs < t, yrs == t
            if te.sum() == 0 or tr.sum() < 10_000:
                continue
            mdl = HistGradientBoostingClassifier(
                max_depth=3, max_iter=200, learning_rate=0.08,
                min_samples_leaf=500, random_state=7).fit(X[tr], y[tr])
            prob[te] = mdl.predict_proba(X[te])[:, 1]
            ypl = y[tr].copy()
            rng.shuffle(ypl)
            mpl = HistGradientBoostingClassifier(
                max_depth=3, max_iter=200, learning_rate=0.08,
                min_samples_leaf=500, random_state=7).fit(X[tr], ypl)
            prob_pl[te] = mpl.predict_proba(X[te])[:, 1]
        oos = np.isfinite(prob)
        auc = roc_auc_score(y[oos], prob[oos])

        def yearly(mask):
            out = []
            for t in (2024, 2025, 2026):
                m = mask & (yrs == t)
                out.append((rr[m] - COST_RT_BP).mean() if m.sum() else np.nan)
            return out

        rows = [
            ("ungated", oos),
            ("gated", oos & (prob >= TAU)),
            ("placebo-gated", oos & (prob_pl >= TAU)),
        ]
        for book, mask in rows:
            net = rr[mask] - COST_RT_BP
            ys = yearly(mask)
            if book == "gated":
                ok_bar = (net.mean() >= 5.0
                          and sum(v > 0 for v in ys if np.isfinite(v)) >= 2)
                verdict = "**RESCUED?**" if ok_bar else "dead"
            else:
                verdict = ""
            label = name if book == "ungated" else ""
            auc_cell = f"{auc:.3f}" if book == "ungated" else ""
            year_cells = " | ".join(f"{v:+.1f}" if np.isfinite(v) else "—"
                                    for v in ys)
            emit(f"| {label} | {book} | {int(mask.sum()):,} "
                 f"| {net.mean():+.1f} | {tstat(net):+.2f} | {year_cells} "
                 f"| {auc_cell} | {verdict} |")
        emit("| | | | | | | | | | |")
    emit()
    emit("Rescue bar: gated net@10 >= +5bp AND >= 2/3 years positive AND "
         "placebo-gated shows no comparable lift.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_canon_rescue.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"canon_rescue_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
