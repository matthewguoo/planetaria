"""Do the candlestick patterns retail traders read mean anything?

The patterns tested are the canon of every trading-influencer thread:
hammer, shooting star, bullish/bearish engulfing, bullish/bearish harami,
morning/evening star, three white soldiers, three black crows, doji. The
academic verdict on the canon is Marshall-Young-Rose (J. Banking & Finance
2006): no value on DJIA stocks 1992-2002, any horizon, any exit rule. This
is the modern replication on the names a retail trader actually trades —
the pead-llm-gate daily panels (top ~1,000 by dollar volume per year,
2016-2026, Adjustment.ALL so every ratio is basis-consistent), with a $50M
prior-day dollar-volume floor and $5 price floor.

Definitions (standard; body = |C-O|, upper/lower = shadows, all ratios of
the day's range; trend = sign of the prior 5-session close-to-close move,
because every reversal pattern is defined AGAINST a trend):

  hammer            downtrend; lower shadow >= 2x body; upper <= 25% body;
                    body in top third of range. Bullish.
  shooting star     uptrend; mirror image. Bearish.
  engulfing (bull)  downtrend; yesterday bearish, today bullish, today's
                    body strictly engulfs yesterday's. Bearish mirrored.
  harami (bull)     downtrend; yesterday big bearish body, today's body
                    inside it. Bearish mirrored.
  morning star      downtrend; big bearish, then small body (<50% of it),
                    then bullish close above the midpoint of bar 1's body.
                    evening star mirrored.
  soldiers/crows    three consecutive same-direction bodies, each closing
                    beyond the last, each body >= its own day's median.
  doji              body <= 10% of range (direction-free; reported for
                    completeness against both sides).

Execution honesty: the signal exists at the close. The naive published form
(enter AT the signal close) is reported, but the REAL line is entry at the
NEXT OPEN, exit at that day's close or T+3's close. Baseline = the same
panel-day universe's mean forward return, so "the market went up" cannot
masquerade as pattern skill. Costs are not charged; read the excess column
against a ~6-13bp round trip.

Run:  python scripts/research_candles.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

MIN_DV = 5e7
MIN_PX = 5.0


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def load_panel() -> pd.DataFrame:
    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "h", "l", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]).reset_index(drop=True))
    g = df.groupby("symbol", sort=False)
    for k in (1, 2):
        for col in ("o", "h", "l", "c"):
            df[f"{col}{k}"] = g[col].shift(k)
    df["dv1"] = (df["c1"] * g["v"].shift(1))
    df["ret5"] = df["c1"] / g["c"].shift(6) - 1     # 5-session trend, ending T-1
    # forward legs
    df["o_next"] = g["o"].shift(-1)
    df["c_next"] = g["c"].shift(-1)
    df["c_fwd3"] = g["c"].shift(-3)
    # gap guard: forward entry must be the NEXT session, not a re-listing
    df["dt"] = pd.to_datetime(df["date"])
    df["fwd_gap"] = (g["dt"].shift(-1) - df["dt"]).dt.days
    return df


def patterns(df: pd.DataFrame) -> dict[str, tuple[np.ndarray, int]]:
    """name -> (mask, direction). Direction +1 = pattern is bullish."""
    o, h, l, c = (df[k].to_numpy() for k in ("o", "h", "l", "c"))
    o1, h1, l1, c1 = (df[f"{k}1"].to_numpy() for k in ("o", "h", "l", "c"))
    o2, c2 = df["o2"].to_numpy(), df["c2"].to_numpy()
    body = np.abs(c - o)
    rng = np.maximum(h - l, 1e-12)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    body1 = np.abs(c1 - o1)
    rel_body = body / np.maximum(c, 1e-12)
    big1 = body1 >= np.nanmedian(rel_body) * np.maximum(c1, 1e-12)
    down = df["ret5"].to_numpy() < 0
    up = df["ret5"].to_numpy() > 0
    bull = c > o
    bear = c < o
    bull1 = c1 > o1
    bear1 = c1 < o1

    out: dict[str, tuple[np.ndarray, int]] = {}
    out["hammer"] = (down & (lower >= 2 * body) & (upper <= 0.25 * body)
                     & (np.minimum(o, c) >= l + 2 / 3 * rng), +1)
    out["shooting star"] = (up & (upper >= 2 * body) & (lower <= 0.25 * body)
                            & (np.maximum(o, c) <= l + 1 / 3 * rng), -1)
    out["bullish engulfing"] = (down & bear1 & bull
                                & (o <= c1) & (c >= o1) & (body > body1), +1)
    out["bearish engulfing"] = (up & bull1 & bear
                                & (o >= c1) & (c <= o1) & (body > body1), -1)
    out["bullish harami"] = (down & bear1 & big1 & bull
                             & (np.maximum(o, c) <= o1)
                             & (np.minimum(o, c) >= c1), +1)
    out["bearish harami"] = (up & bull1 & big1 & bear
                             & (np.maximum(o, c) <= c1)
                             & (np.minimum(o, c) >= o1), -1)
    small1 = body1 <= 0.5 * np.abs(c2 - o2)
    out["morning star"] = (down & (c2 < o2) & small1 & bull & (c >= (o2 + c2) / 2)
                           & (np.abs(c2 - o2) > 0), +1)
    out["evening star"] = (up & (c2 > o2) & small1 & bear & (c <= (o2 + c2) / 2)
                           & (np.abs(c2 - o2) > 0), -1)
    body_frac1 = body1 >= 0.6 * np.maximum(h1 - l1, 1e-12)
    out["three white soldiers"] = (bull & bull1 & (c2 > o2) & (c > c1) & (c1 > c2)
                                   & (body >= 0.6 * rng) & body_frac1, +1)
    out["three black crows"] = (bear & bear1 & (c2 < o2) & (c < c1) & (c1 < c2)
                                & (body >= 0.6 * rng) & body_frac1, -1)
    out["doji"] = (body <= 0.10 * rng, 0)
    return out


def main() -> None:
    df = load_panel()
    eligible = ((df["dv1"] >= MIN_DV) & (df["c1"] >= MIN_PX)
                & df["ret5"].notna() & df["o_next"].notna()
                & df["c_next"].notna() & (df["fwd_gap"] <= 5))
    df = df[eligible].reset_index(drop=True)

    # Signed forward returns per execution convention.
    naive = df["c_next"] / df["c"] - 1              # published form: at signal close
    real1 = df["c_next"] / df["o_next"] - 1         # next open -> next close
    real3 = df["c_fwd3"] / df["o_next"] - 1         # next open -> T+3 close

    base = {"naive": naive.mean() * 1e4, "real1": real1.mean() * 1e4,
            "real3": np.nanmean(real3) * 1e4}

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Candlestick patterns on the liquid panel — {STAMP}")
    emit()
    emit(f"{len(df):,} eligible symbol-days (dv >= ${MIN_DV / 1e6:.0f}M, "
         f"px >= ${MIN_PX:.0f}, 2016-2026). Signed = pattern direction "
         "(short-side patterns flip sign; the account is long-biased, so "
         "read bearish rows as avoid/exit signals). Baseline row = the same "
         "universe unconditioned; the excess column is the pattern's claim. "
         f"Universe baselines: naive {base['naive']:+.1f}bp, next-open "
         f"{base['real1']:+.1f}bp, 3-day {base['real3']:+.1f}bp. No costs "
         "charged; a round trip is 6-13bp.")
    emit()
    emit("| pattern | n | naive bp | xs | open->close bp | xs | t(xs) | open->T+3 bp | xs | 21-26 hit% |")
    emit("|---|---|---|---|---|---|---|---|---|---|")

    pats = patterns(df)
    late = (df["date"] >= "2021-01-01").to_numpy()
    for name, (mask, direction) in pats.items():
        m = mask & np.isfinite(naive.to_numpy())
        n = int(m.sum())
        if n < 100:
            emit(f"| {name} | {n} | - | - | - | - | - | - | - | - |")
            continue
        s = direction if direction != 0 else 1
        nv = s * naive.to_numpy()[m] * 1e4
        r1 = s * real1.to_numpy()[m] * 1e4
        r3 = s * np.asarray(real3)[m] * 1e4
        # excess vs the signed baseline (bearish patterns beat a FALLING
        # baseline only if the stock falls MORE than the universe rose)
        xs_nv = nv.mean() - s * base["naive"]
        xs_r1 = r1.mean() - s * base["real1"]
        xs_r3 = np.nanmean(r3) - s * base["real3"]
        # t on the excess of the realistic 1-day leg
        t_r1 = tstat(r1 - s * base["real1"])
        hit = (r1[late[m]] > 0).mean() * 100 if late[m].any() else float("nan")
        emit(f"| {name} | {n} | {nv.mean():+.1f} | {xs_nv:+.1f} "
             f"| {r1.mean():+.1f} | {xs_r1:+.1f} | {t_r1:+.2f} "
             f"| {np.nanmean(r3):+.1f} | {xs_r3:+.1f} | {hit:.1f} |")
    emit()
    emit("doji is direction-free and shown long-signed for completeness.")
    emit()
    emit("Read against Marshall-Young-Rose (JBF 2006): candlesticks added "
         "no value on 1992-2002 DJIA data. The columns above are the same "
         "question on 2016-2026 liquid names, with the entry you could "
         "actually take (the next open).")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"candles_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
