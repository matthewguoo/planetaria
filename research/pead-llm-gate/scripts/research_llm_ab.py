"""LLM-layer A/B backtest: fable vs opus on real historical releases.

Runs the LIVE strategy's exact analysis setup — SURPRISE_SCHEMA and the
HARDENED_SYSTEM preamble imported from the running code, task text
mirroring earnings_reaction._decide_text (consensus line reads "unknown":
free Finnhub keeps no history; live sees the same for ~34%% of names) —
over a stratified sample of gate-5%% PEAD events, once per model arm
(claude-fable-5, claude-opus-5), through the SAME ClaudeCliClient the
engine uses on subscription auth.

Honesty rails baked in:
- CONTAMINATION SLICING: months before ~fable's knowledge cutoff
  (2026-02) are in-corpus — the model may REMEMBER outcomes. Results are
  reported pre/post cutoff separately; only post-cutoff is evidence.
- PAIRED DESIGN: both arms see identical events; scoring compares each
  arm's verdict-gated subset against the mechanical baseline on the same
  trades (fwd returns from the cached PEAD panel, same costs).
- CHECKPOINTED: every verdict appends to a jsonl; reruns resume; 429s
  back off 5 minutes (subscription window exhaustion is expected on big
  samples — the run is built to survive the night).
- Effort note: the CLI exposes no effort knob; both arms run at model
  defaults (the live 'medium' applies only via the API backend).

Run:  .venv/Scripts/python.exe scripts/research_llm_ab.py --n 150
      [--models claude-fable-5,claude-opus-5] [--score-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time as _time
from datetime import datetime

from _paths import BACKEND, CACHE, NOTES
from zoneinfo import ZoneInfo

sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.llm import HARDENED_SYSTEM, ClaudeCliClient  # noqa: E402
from app.services.signals.edgar import EdgarFeed, strip_html  # noqa: E402
from app.strategies.pead_flagship import SURPRISE_SCHEMA  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = CACHE
TEXTS = CACHE / "texts"
OUT = CACHE / "llm_ab"
SEC_UA = {"User-Agent": "planetaria/0.1 (contact: matthewguo.x86@gmail.com)"}

GATE = 5.0
COSTS = 13.0
CUTOFF_MONTH = "2026-02"   # fable knowledge boundary: >= this is OOS
MAX_TEXT = 12_000
CONCURRENCY = 3


# ------------------------------------------------------------------ data

WINDOWS = {
    "2025": ("pead_events_2025-02-01_2026-08-01_2873.parquet", "2025-01"),
    "2022": ("pead_events_2022-01-01_2022-12-31_*.parquet", "2021-12"),
    "2023": ("pead_events_2023-01-01_2023-12-31_*.parquet", "2022-12"),
}


def load_events(window: str, top_per_day: int, min_dv: float) -> pd.DataFrame:
    pat, bars_tag = WINDOWS[window]
    ev = pd.read_parquet(sorted(CACHE.glob(pat))[0])
    ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
    ev["fwd_bp"] = (ev["exit"] / ev["react"] - 1) * 1e4
    ev["month"] = ev["date"].astype(str).str[:7]
    g = ev[np.abs(ev["move_pct"]) >= GATE].copy()
    bars = pd.read_parquet(
        [f for f in CACHE.glob("bars_ohlc_*") if bars_tag in f.name][0])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["symbol", "date"])
    panel = {s: (gg["date"].to_list(), gg["c"].to_numpy(),
                 (gg["c"] * gg["v"]).to_numpy())
             for s, gg in bars.groupby("symbol")}

    def ctx(row):
        d, c, dv = panel.get(row["symbol"], (None, None, None))
        if d is None:
            return pd.Series({"run5d": np.nan, "dv": np.nan})
        i = np.searchsorted(d, pd.to_datetime(row["date"]).date())
        if not (6 <= i < len(c)):
            return pd.Series({"run5d": np.nan, "dv": np.nan})
        return pd.Series({"run5d": (c[i - 1] / c[i - 6] - 1) * 100,
                          "dv": dv[i]})

    g = pd.concat([g, g.apply(ctx, axis=1)], axis=1)
    # Live-matching universe: per announce-day, the top_per_day most liquid
    # reporters above the floor — the watchlist rule, applied historically.
    g = g[g["dv"] >= min_dv]
    g = (g.sort_values("dv", ascending=False)
         .groupby("date").head(top_per_day))
    return g.reset_index(drop=True)


def stratified_sample(g: pd.DataFrame, n: int) -> pd.DataFrame:
    """Proportional by month, balanced by direction inside each month."""
    rng = np.random.default_rng(20260805)
    out = []
    per_month = max(2, n // g["month"].nunique())
    for _, mg in g.groupby("month"):
        for _, dg in mg.groupby(np.sign(mg["move_pct"])):
            k = min(len(dg), max(1, per_month // 2))
            out.append(dg.sample(n=k, random_state=int(rng.integers(1e9))))
    df = pd.concat(out).drop_duplicates(subset=["symbol", "date"])
    if len(df) > n:
        df = df.sample(n=n, random_state=7)
    return df.reset_index(drop=True)


def fetch_release_text(sym: str, cik: int, day: str,
                       http: httpx.Client) -> str | None:
    """8-K EX-99 text for the event date, via submissions -> index -> exhibit
    (the live feed's own resolution logic)."""
    TEXTS.mkdir(parents=True, exist_ok=True)
    cache = TEXTS / f"{sym}_{day}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace") or None
    try:
        recent = http.get(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        ).json()["filings"]["recent"]
        acc = None
        for i in range(len(recent["form"])):
            if recent["form"][i] == "8-K" and recent["filingDate"][i] == day \
                    and "2.02" in (recent.get("items") or [""] * 99999)[i]:
                acc = recent["accessionNumber"][i]
                break
        if acc is None:
            cache.write_text("", encoding="utf-8")
            return None
        nodash = acc.replace("-", "")
        idx = http.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                       f"{nodash}/{acc}-index.htm").text
        path = EdgarFeed._find_exhibit(idx)
        if path is None:
            cache.write_text("", encoding="utf-8")
            return None
        url = path if path.startswith("http") else f"https://www.sec.gov{path}"
        text = strip_html(http.get(url).text)[:MAX_TEXT]
        cache.write_text(text, encoding="utf-8")
        return text or None
    except Exception:
        return None


def build_task(sym: str, day_move: float | None, run5d: float | None) -> str:
    """Mirrors earnings_reaction._decide_text task construction; consensus
    unavailable historically (disclosed deviation)."""
    setup_bits = []
    if day_move is not None and np.isfinite(day_move):
        setup_bits.append(f"{day_move:+.1f}% today into the print")
    if run5d is not None and np.isfinite(run5d):
        setup_bits.append(f"{run5d:+.1f}% over the prior 5 sessions")
    setup_line = (
        f" Setup into the print: {', '.join(setup_bits)}. Weigh whether "
        f"these results were already priced in by that run-up."
        if setup_bits else ""
    )
    return (
        f"{sym} released quarterly results after the close. Street "
        f"consensus for the quarter: EPS unknown, revenue unknown. "
        f"Extract the reported figures and guidance from the release, "
        f"compare against that consensus, and judge near-term direction."
        f"{setup_line}"
    )


# ------------------------------------------------------------------- run

async def run_arm(model: str, window: str, sample: pd.DataFrame,
                  tick2cik: dict[str, int]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 2025 keeps the legacy filename so the first sampled run's verdicts
    # are reused as checkpoints.
    out_path = OUT / (f"{model}.jsonl" if window == "2025"
                      else f"{model}_{window}.jsonl")
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done.add((r["symbol"], r["date"]))
            except Exception:
                pass
    client = ClaudeCliClient()
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    async def one(row) -> None:
        key = (row["symbol"], str(row["date"]))
        if key in done:
            return
        cik = tick2cik.get(row["symbol"])
        if cik is None:
            return
        with httpx.Client(headers=SEC_UA, timeout=30) as http:
            text = fetch_release_text(row["symbol"], cik, str(row["date"]), http)
        if not text:
            return
        task = build_task(row["symbol"], None, row.get("run5d"))
        prompt = f"{task}\n\n<data>\n{text}\n</data>"
        async with sem:
            for attempt in range(4):
                try:
                    resp = await client.messages.create(
                        model=model, max_tokens=2048, system=HARDENED_SYSTEM,
                        output_config={"format": {"type": "json_schema",
                                                  "schema": SURPRISE_SCHEMA}},
                        messages=[{"role": "user", "content": prompt}])
                    verdict = json.loads(resp.content[0].text)
                    async with lock:
                        with out_path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "symbol": row["symbol"], "date": str(row["date"]),
                                "model": model, "verdict": verdict}) + "\n")
                    return
                except Exception as exc:
                    msg = str(exc)[:100]
                    wait = 300.0 if ("429" in msg or "rate" in msg.lower()
                                     or "limit" in msg.lower()) else 20.0
                    print(f"  [{model}] {row['symbol']} attempt {attempt}: "
                          f"{msg} (sleep {wait:.0f}s)")
                    await asyncio.sleep(wait)

    t0 = _time.monotonic()
    await asyncio.gather(*(one(row) for _, row in sample.iterrows()))
    print(f"[{model}] arm complete in {(_time.monotonic() - t0)/60:.1f} min")


def score(sample: pd.DataFrame) -> None:
    frames = []
    for path in OUT.glob("*.jsonl"):
        rows = [json.loads(x) for x in
                path.read_text(encoding="utf-8").splitlines()]
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        print("no verdicts yet")
        return
    v = pd.concat(frames)
    v["direction"] = v["verdict"].apply(lambda x: x.get("direction"))
    v["confidence"] = v["verdict"].apply(lambda x: x.get("confidence"))
    m = sample.copy()
    m["date"] = m["date"].astype(str)
    merged = v.merge(m, on=["symbol", "date"], how="inner")
    merged["mech_pnl"] = np.sign(merged["move_pct"]) * merged["fwd_bp"] - COSTS
    merged["oos"] = merged["month"] >= CUTOFF_MONTH

    print("\n=== LLM A/B vs mechanical baseline (same events, paired) ===")
    for model, g in merged.groupby("model"):
        for oos, gg in g.groupby("oos"):
            tag = "OOS (post-cutoff)" if oos else "IN-CORPUS (tainted)"
            agree = gg[((gg["direction"] == "bullish") & (gg["move_pct"] > 0)) |
                       ((gg["direction"] == "bearish") & (gg["move_pct"] < 0))]
            veto = gg.drop(agree.index)
            base = gg["mech_pnl"].mean()
            print(f"{model:16s} {tag:20s} n={len(gg):3d} | mech {base:+7.1f}bp"
                  f" | LLM-gated n={len(agree):3d} {agree['mech_pnl'].mean():+7.1f}bp"
                  f" | vetoed n={len(veto):3d} {veto['mech_pnl'].mean():+7.1f}bp")
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    doc = (NOTES
           / f"llm_ab_{stamp}.md")
    doc.write_text(f"# LLM A/B — {stamp}\n\nSee script docstring for method; "
                   f"raw verdicts in scripts/_leadup_cache/llm_ab/.\n",
                   encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=0,
                    help="sample size; 0 = ALL selected events")
    ap.add_argument("--window", default="2025", choices=list(WINDOWS))
    ap.add_argument("--top-per-day", type=int, default=5)
    ap.add_argument("--min-dv", type=float, default=5e7)
    ap.add_argument("--models", default="claude-fable-5,claude-opus-5")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()

    g = load_events(args.window, args.top_per_day, args.min_dv)
    sample = stratified_sample(g, args.n) if args.n else g
    print(f"[{args.window}] gate-{GATE:.0f}% top-{args.top_per_day}/day "
          f"dv>=${args.min_dv/1e6:.0f}M: {len(g)} events; running "
          f"{len(sample)} ({(sample['month'] >= CUTOFF_MONTH).sum()} post-cutoff)")
    if not args.score_only:
        with httpx.Client(headers=SEC_UA, timeout=30) as http:
            rows = http.get("https://www.sec.gov/files/company_tickers.json").json()
        tick2cik = {}
        for r in rows.values():
            tick2cik.setdefault(str(r["ticker"]).upper(), int(r["cik_str"]))
        for model in args.models.split(","):
            asyncio.run(run_arm(model.strip(), args.window, sample, tick2cik))
    score(sample)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
