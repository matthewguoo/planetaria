"""5-year Opus replication of the LLM gate, with the contamination question
attacked directly instead of sliced around.

THE PROBLEM. The fable harness (research_llm_ab.py) handled training-data
contamination by cutting at the knowledge boundary and reporting only the
post-cutoff slice. That works for a 6-month study. It does not work here:
Opus 5's corpus runs to ~2026-05, so four of these five years are
in-corpus by construction. Cutoff-slicing would throw away the study.
So the controls have to measure the contamination channel rather than
avoid it.

THREE ARMS, one request per event, no shared context:

  named  — the live prompt. Ticker named, real 8-K text, consensus
           "unknown" (matching live: free Finnhub keeps no history).
           This is the replication.
  blind  — IDENTITY ABLATION. Ticker, company name, exchange tags, URLs and
           every date/quarter/fiscal-year reference scrubbed from the task
           AND the text; figures and their comparatives left untouched
           because that IS the signal. If the edge survives here it came
           from reading, not from remembering. The arm also asks the model
           to NAME the company it thinks it is reading — the
           re-identification rate is the honest measure of how leaky the
           scrub is, reported rather than assumed away.
  notext — MEMORY PROBE. Ticker and date only, no release at all, asking
           for the same direction call plus explicit outcome recall. There
           is no information in this prompt, so ANY edge here is pure
           memorisation. It is the direct upper bound on what contamination
           can be worth, and it is the cheapest arm.

FOUR MORE RAILS:

  - CONTAMINATION GRADIENT. Memorisation should not be flat in time: older
    events are more discussed and more repeated in a corpus. The per-year
    edge and an OLS slope of gated P&L on event age are reported. A flat
    profile across five years is evidence against a memorisation mechanism;
    a rising-with-age profile is evidence for one.
  - PERMUTATION NULL. Verdicts are shuffled within month, 1000x, and the
    gate re-scored. This gives an exact null for "verdicts carry no
    event-specific information" — no t-stat assumptions.
  - NO RETRIEVAL. Batch requests declare no tools; the model cannot look
    the answer up. Asserted in code, not just intended.
  - NO LOOKAHEAD. The prompt never contains the tape reaction, the forward
    return, or anything dated after the release. Selection (universe,
    liquidity rank) uses prior-session data only.

RESIDUAL LIMITATION, stated because it cannot be engineered away: a model
trained on text written after these events carries regime priors about
these companies even when it recalls no specific release. `blind` bounds
this; it does not remove it.

Run (staged; each stage prints its cost estimate and refuses to exceed
--budget):
    python scripts/research_llm_contamination.py texts        # EDGAR fetch
    python scripts/research_llm_contamination.py calibrate    # low vs medium
    python scripts/research_llm_contamination.py submit --arm named
    python scripts/research_llm_contamination.py collect
    python scripts/research_llm_contamination.py score
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time as _time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services.llm import HARDENED_SYSTEM  # noqa: E402
from app.services.signals.edgar import EdgarFeed, strip_html  # noqa: E402
from app.strategies.earnings_reaction import SURPRISE_SCHEMA  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = Path(__file__).resolve().parent / "_leadup_cache"
TEXTS = CACHE / "texts"
OUT = CACHE / "llm_contam"
SEC_UA = {"User-Agent": "planetaria/0.1 (contact: matthewguo.x86@gmail.com)"}

MODEL = "claude-opus-5"
GATE = 5.0          # |reaction| gate, percent — the live min_move_pct
TOP_PER_DAY = 5     # the live watchlist rule, applied historically
MIN_DV = 5e7        # liquidity floor, prior-session dollar volume
COSTS_BP = 13.0     # 10bp AH entry + 3bp RTH exit
MAX_TEXT = 12_000   # the live max_text
N_PERM = 1000

# Batch API list price, USD per million tokens (50% of standard).
PRICE_IN, PRICE_OUT = 2.50, 12.50
EST_OUT_TOKENS = {"low": 700, "medium": 1400}   # incl. thinking; refined by
                                                # measured usage after run 1


# --------------------------------------------------------------- selection

def _bars_for(lo: str, hi: str) -> pd.DataFrame:
    """The daily panel COVERING [lo, hi]. Matching on a substring of the
    filename (the old harness's approach) silently picked the 2022 panel
    for the 2023 window — every dv/run5d came back NaN, the liquidity floor
    dropped every row, and the arm scored zero events. Match on the range."""
    for path in sorted(CACHE.glob("bars_ohlc_*.parquet")):
        span = re.findall(r"(\d{4}-\d{2}-\d{2})", path.name)[:2]
        if len(span) == 2 and span[0] <= lo and span[1] >= hi:
            return pd.read_parquet(path)
    raise SystemExit(f"no cached OHLC panel covering {lo}..{hi}")


def load_universe() -> pd.DataFrame:
    """Every gate-5% event in the five cached windows, reduced to the LIVE
    watchlist rule: per announce-day, the top-N most liquid reporters above
    the dollar-volume floor."""
    frames = []
    for path in sorted(CACHE.glob("pead_events_*.parquet")):
        ev = pd.read_parquet(path)
        ev["date"] = pd.to_datetime(ev["date"]).dt.date
        ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
        ev["fwd_bp"] = (ev["exit"] / ev["react"] - 1) * 1e4
        g = ev[np.abs(ev["move_pct"]) >= GATE].copy()
        if g.empty:
            continue
        lo, hi = str(g["date"].min()), str(g["date"].max())
        bars = _bars_for(lo, hi)
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        bars = bars.sort_values(["symbol", "date"])
        panel = {s: (gg["date"].to_list(), gg["c"].to_numpy(),
                     (gg["c"] * gg["v"]).to_numpy())
                 for s, gg in bars.groupby("symbol")}

        def ctx(row):
            d, c, dv = panel.get(row["symbol"], (None, None, None))
            if d is None:
                return pd.Series({"run5d": np.nan, "dv": np.nan})
            i = np.searchsorted(d, row["date"])
            if not (6 <= i < len(c)):
                return pd.Series({"run5d": np.nan, "dv": np.nan})
            # Strictly prior sessions: no lookahead into the event day.
            return pd.Series({"run5d": (c[i - 1] / c[i - 6] - 1) * 100,
                              "dv": dv[i - 1]})

        g = pd.concat([g, g.apply(ctx, axis=1)], axis=1)
        g = g[g["dv"] >= MIN_DV]
        g = g.sort_values("dv", ascending=False).groupby("date").head(TOP_PER_DAY)
        frames.append(g)
    out = pd.concat(frames, ignore_index=True)
    out["month"] = out["date"].astype(str).str[:7]
    out["year"] = out["date"].astype(str).str[:4]
    return out.drop_duplicates(subset=["symbol", "date"]).sort_values("date")


# ------------------------------------------------------------------ texts

def _accessions(cik: int, http: httpx.Client) -> list[dict]:
    """Every 8-K in the filer's history, newest shard first. `filings.recent`
    holds only the last ~1000 filings — for a prolific filer that does not
    reach 2021, which would silently bias the oldest window toward quiet
    companies. Walk the older shards too."""
    rows: list[dict] = []
    try:
        doc = http.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
    except Exception:
        return rows
    shards = [doc["filings"]["recent"]]
    for extra in doc["filings"].get("files") or []:
        try:
            shards.append(http.get(
                f"https://data.sec.gov/submissions/{extra['name']}").json())
        except Exception:
            continue
        _time.sleep(0.12)
    for shard in shards:
        items = shard.get("items") or [""] * len(shard["form"])
        for i in range(len(shard["form"])):
            if shard["form"][i] == "8-K" and "2.02" in (items[i] or ""):
                rows.append({"date": shard["filingDate"][i],
                             "acc": shard["accessionNumber"][i]})
    return rows


def fetch_release_text(sym: str, cik: int, day: str,
                       http: httpx.Client) -> str | None:
    """8-K EX-99 text for the event date (the live feed's own resolution
    logic). Cached; an empty cache file means 'looked, found nothing' and is
    not retried."""
    TEXTS.mkdir(parents=True, exist_ok=True)
    cache = TEXTS / f"{sym}_{day}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace") or None
    acc = next((r["acc"] for r in _accessions(cik, http) if r["date"] == day), None)
    if acc is None:
        cache.write_text("", encoding="utf-8")
        return None
    try:
        nodash = acc.replace("-", "")
        idx = http.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                       f"{nodash}/{acc}-index.htm").text
        path = EdgarFeed._find_exhibit(idx)
        if path is None:
            cache.write_text("", encoding="utf-8")
            return None
        url = path if path.startswith("http") else f"https://www.sec.gov{path}"
        text = strip_html(http.get(url).text)[:MAX_TEXT]
    except Exception:
        return None            # transient: leave uncached so it retries
    cache.write_text(text, encoding="utf-8")
    return text or None


def ticker_map(http: httpx.Client) -> tuple[dict[str, int], dict[str, str]]:
    rows = http.get("https://www.sec.gov/files/company_tickers.json").json()
    cik, name = {}, {}
    for r in rows.values():
        t = str(r["ticker"]).upper()
        cik.setdefault(t, int(r["cik_str"]))
        name.setdefault(t, str(r.get("title") or ""))
    return cik, name


# ----------------------------------------------------------- anonymisation

_STOP = {"INC", "CORP", "CORPORATION", "COMPANY", "CO", "LTD", "LIMITED",
         "PLC", "HOLDINGS", "HOLDING", "GROUP", "THE", "AND", "CLASS",
         "COMMON", "STOCK", "TECHNOLOGIES", "TECHNOLOGY", "INTERNATIONAL",
         "SYSTEMS", "SERVICES", "SOLUTIONS", "PARTNERS", "TRUST", "N.V.",
         "S.A.", "AG", "SE", "LLC", "LP"}

_MONTHS = ("January|February|March|April|May|June|July|August|September|"
           "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
           "Oct|Nov|Dec")


def anonymise(text: str, symbol: str, company: str) -> tuple[str, int]:
    """Strip identity and time, keep the numbers. Returns (text, n_subs) so
    a scrub that did nothing is visible rather than silent."""
    n = 0

    def sub(pattern, repl, s, flags=re.I):
        nonlocal n
        s, k = re.subn(pattern, repl, s, flags=flags)
        n += k
        return s

    # Exchange tags first: "(Nasdaq: AMD)" would otherwise leave a bare ticker.
    text = sub(r"\((?:nasdaq|nyse|nyse american|otc|cboe|amex)\s*:\s*[A-Z.\-]{1,6}\)",
               "(the Company)", text)
    # EDGAR's exhibit header carries the filename, which is usually the
    # ticker: "EX-99.1 2 aaoi_ex9901.htm PRESS RELEASE". strip_html returns
    # ONE line, so this must be length-bounded — an unbounded [^\n]* ate the
    # entire document and silently emptied the blind arm (caught by the
    # scrub audit, 2026-08-06, before a cent was spent).
    text = sub(r"^EX-99[^\n]{0,120}?\.htm\w*\s*", "", text[:200]) + text[200:]
    # Underscores are word characters, so \bAAOI\b does NOT match "aaoi_ex99".
    # Use explicit alphanumeric boundaries.
    text = sub(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])",
               "the Company", text)
    # Self-declared short forms: ("AOI"), ("Alphabet"). Companies introduce
    # an abbreviation once and then use it throughout.
    text = sub(r"[(“\"']\s*[“\"']?([A-Z][A-Za-z]{1,14})[”\"']?\s*[)”\"']",
               "(the Company)", text, flags=0)
    if company:
        text = sub(re.escape(company), "the Company", text)
        for token in re.split(r"[^A-Za-z0-9']+", company):
            if len(token) > 3 and token.upper() not in _STOP:
                text = sub(rf"\b{re.escape(token)}\b", "the Company", text)
    # Time: absolute years, quarter/fiscal labels, month-day-year dates.
    text = sub(rf"\b(?:{_MONTHS})\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}\b", "[date]", text)
    text = sub(rf"\b(?:{_MONTHS})\s+(?:19|20)\d{{2}}\b", "[date]", text)
    text = sub(r"\b(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?"
               r"(?:fiscal\s+)?(?:19|20)\d{2}\b", "the quarter", text)
    text = sub(r"\bQ([1-4])\s*(?:FY)?\s*'?(?:19|20)?\d{2}\b", r"Q\1", text)
    text = sub(r"\b(?:fiscal|calendar)\s+(?:year\s+)?(?:19|20)\d{2}\b",
               "the fiscal year", text)
    text = sub(r"\b(?:19|20)\d{2}\b", "[year]", text)
    text = sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "[date]", text)
    # Contact furniture: URLs, emails, phone numbers, IR boilerplate.
    text = sub(r"https?://\S+|www\.\S+", "[url]", text)
    text = sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    text = sub(r"\+?\d{1,2}[\s.\-(]*\d{3}[\s.\-)]*\d{3}[\s.\-]*\d{4}\b", "[phone]", text)
    return text, n


# -------------------------------------------------------------- prompting

BLIND_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: v for k, v in SURPRISE_SCHEMA["properties"].items()},
        # The leak gauge: how often the scrub fails to de-identify.
        "guessed_company": {"type": "string"},
        "identification_confidence": {
            "type": "string", "enum": ["none", "low", "medium", "high"]},
    },
    "required": [*SURPRISE_SCHEMA["required"], "guessed_company",
                 "identification_confidence"],
    "additionalProperties": False,
}

NOTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "recalls_event": {"type": "string", "enum": ["yes", "no"]},
        "recalled_next_session_direction": {
            "type": "string", "enum": ["up", "down", "unknown"]},
        "direction": {"type": "string",
                      "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "basis": {"type": "string"},
    },
    "required": ["recalls_event", "recalled_next_session_direction",
                 "direction", "confidence", "basis"],
    "additionalProperties": False,
}

BLIND_SYSTEM = (
    HARDENED_SYSTEM + "\n\nThe issuer's identity and all dates have been "
    "removed from this release. Judge it on its contents alone. Separately, "
    "report which company you believe it is and how confident you are — an "
    "honest 'unknown' with identification_confidence 'none' is the correct "
    "answer when the text does not tell you."
)

NOTEXT_SYSTEM = (
    HARDENED_SYSTEM + "\n\nYou are given no release text. This is a "
    "calibration probe of what you remember, not a reasoning task. Answer "
    "from recall only. If you do not specifically remember this company's "
    "report on this date, say so: recalls_event 'no' and "
    "recalled_next_session_direction 'unknown' are the correct answers, and "
    "guessing is worse than admitting the gap."
)


def task_named(sym: str, run5d: float | None) -> str:
    """Mirrors earnings_reaction._decide_text. Consensus reads 'unknown'
    because that is what live sees for ~34% of names (free Finnhub keeps no
    history) — a disclosed deviation, not an oversight. The tape reaction is
    NOT in the prompt: the model must not see the answer it is gating."""
    setup = ""
    if run5d is not None and np.isfinite(run5d):
        setup = (f" Setup into the print: {run5d:+.1f}% over the prior 5 "
                 f"sessions. Weigh whether these results were already priced "
                 f"in by that run-up.")
    return (f"{sym} released quarterly results after the close. Street "
            f"consensus for the quarter: EPS unknown, revenue unknown. "
            f"Extract the reported figures and guidance from the release, "
            f"compare against that consensus, and judge near-term "
            f"direction.{setup}")


def task_blind(run5d: float | None) -> str:
    setup = ""
    if run5d is not None and np.isfinite(run5d):
        setup = (f" Setup into the print: the shares moved {run5d:+.1f}% over "
                 f"the prior 5 sessions. Weigh whether these results were "
                 f"already priced in by that run-up.")
    return ("A company released quarterly results after the close. Street "
            "consensus for the quarter: EPS unknown, revenue unknown. "
            "Extract the reported figures and guidance from the release, "
            "compare against that consensus, and judge near-term "
            f"direction.{setup}")


def task_notext(sym: str, day: str) -> str:
    return (f"{sym} released quarterly results after the close on {day}. You "
            f"are given no release text and no market data. From memory "
            f"alone: do you specifically recall this report, which way did "
            f"the shares trade in the next full session, and what direction "
            f"would you call?")


def build_request(row, arm: str, text: str | None, company: str,
                  effort: str) -> dict | None:
    """One self-contained request. No conversation history, no shared
    session, no cross-event state — every decision is a fresh one."""
    # effort is part of the key: the calibration A/B runs the SAME event
    # at two efforts, and a key without it would silently dedupe the second.
    # The Batch API restricts custom_id to ^[a-zA-Z0-9_-]{1,64}$ — no pipes,
    # no dots, no colons. Underscores and a compact date it is.
    key = f"{arm}_{effort}_{row['symbol']}_{str(row['date']).replace('-', '')}"
    run5d = row.get("run5d")
    if arm == "notext":
        system, schema = NOTEXT_SYSTEM, NOTEXT_SCHEMA
        content = task_notext(row["symbol"], str(row["date"]))
    elif arm == "blind":
        if not text:
            return None
        scrubbed, subs = anonymise(text, row["symbol"], company)
        if subs == 0:
            return None      # nothing was scrubbed: not a blind sample
        system, schema = BLIND_SYSTEM, BLIND_SCHEMA
        content = f"{task_blind(run5d)}\n\n<data>\n{scrubbed}\n</data>"
    else:
        if not text:
            return None
        system, schema = HARDENED_SYSTEM, SURPRISE_SCHEMA
        content = f"{task_named(row['symbol'], run5d)}\n\n<data>\n{text}\n</data>"
    return {
        "custom_id": key,
        "params": {
            "model": MODEL,
            "max_tokens": 4096,
            "system": system,
            # NO tools: the model cannot look the outcome up. This absence is
            # load-bearing, not an omission.
            "output_config": {"format": {"type": "json_schema",
                                         "schema": schema},
                              "effort": effort},
            "messages": [{"role": "user", "content": content}],
        },
    }


# ----------------------------------------------------------------- costing

def estimate_cost(requests: list[dict], effort: str) -> tuple[float, int]:
    """Char-based input estimate (3.6 chars/token on dense financial prose)
    plus a measured-output prior. Deliberately rough and deliberately HIGH:
    the budget guard should trip early, not late."""
    chars = sum(len(r["params"]["system"])
                + len(r["params"]["messages"][0]["content"]) for r in requests)
    tok_in = chars / 3.6
    tok_out = len(requests) * EST_OUT_TOKENS.get(effort, 1000)
    usd = tok_in / 1e6 * PRICE_IN + tok_out / 1e6 * PRICE_OUT
    return usd, int(tok_in)


def client():
    """API key from the SAME .env the engine reads, passed explicitly. The
    engine itself stays on LLM_BACKEND=claude-cli — nothing in app/ gains a
    dependency on the API because of this study."""
    import os

    import anthropic

    from app.config import get_settings

    # ANTHROPIC_API_KEY is the name app/config knows; CLAUDE_API_KEY is
    # accepted here too so the key does not have to be renamed for a
    # research script. Deliberately read in THIS file, not in app/config —
    # the engine must not grow a dependency on the API because of a study.
    key = os.environ.get("ANTHROPIC_API_KEY") or get_settings().anthropic_api_key
    if not key:
        env = Path(__file__).resolve().parents[2] / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"):
                    key = value.strip().strip('"').strip("'")
                    break
    if not key:
        raise SystemExit("no API key in .env (ANTHROPIC_API_KEY or "
                         "CLAUDE_API_KEY) — this study is the only thing that "
                         "needs one; the engine is unaffected.")
    return anthropic.Anthropic(api_key=key)


# ------------------------------------------------------------------ stages

def stage_texts(args) -> None:
    universe = load_universe()
    print(f"{len(universe)} selected events "
          f"{universe['date'].min()}..{universe['date'].max()}")
    with httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True) as http:
        cik, _ = ticker_map(http)
        have = miss = 0
        for n, (_, row) in enumerate(universe.iterrows()):
            c = cik.get(row["symbol"])
            if c is None:
                miss += 1
                continue
            text = fetch_release_text(row["symbol"], c, str(row["date"]), http)
            have += bool(text)
            miss += not text
            if n % 50 == 0:
                print(f"  texts {n}/{len(universe)} have={have} miss={miss}")
            _time.sleep(0.12)
    print(f"release texts: {have} have / {miss} missing of {len(universe)}")


def _requests_for(arm: str, universe: pd.DataFrame, effort: str,
                  limit: int | None) -> list[dict]:
    with httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True) as http:
        _, names = ticker_map(http)
    done = _completed_ids()
    requests = []
    for _, row in universe.iterrows():
        key = (f"{arm}_{effort}_{row['symbol']}_"
               f"{str(row['date']).replace('-', '')}")
        if key in done:
            continue
        text = None
        if arm != "notext":
            path = TEXTS / f"{row['symbol']}_{row['date']}.txt"
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
            if not text:
                continue
        req = build_request(row, arm, text, names.get(row["symbol"], ""), effort)
        if req:
            requests.append(req)
        if limit and len(requests) >= limit:
            break
    return requests


def _completed_ids() -> set[str]:
    OUT.mkdir(parents=True, exist_ok=True)
    done = set()
    for path in OUT.glob("results_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["custom_id"])
            except Exception:
                pass
    return done


def stage_submit(args) -> None:
    universe = load_universe()
    if args.window:
        universe = universe[universe["year"].isin(args.window.split(","))]
    requests = _requests_for(args.arm, universe, args.effort, args.limit)
    if not requests:
        print("nothing to submit (all done, or no texts)")
        return
    usd, tok_in = estimate_cost(requests, args.effort)
    print(f"arm={args.arm} effort={args.effort}: {len(requests)} requests, "
          f"~{tok_in / 1e6:.2f}M input tokens, EST ${usd:.2f} (batch price)")
    spent = _spent_so_far()
    print(f"already spent this study: ${spent:.2f}; budget ${args.budget:.2f}")
    if spent + usd > args.budget:
        raise SystemExit(f"REFUSED: would take the study to ${spent + usd:.2f}, "
                         f"past the ${args.budget:.2f} budget. Narrow with "
                         f"--limit or --window, or raise --budget deliberately.")
    if args.dry_run:
        print("dry run — nothing submitted")
        return
    batch = client().messages.batches.create(requests=requests)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "batches.jsonl").open("a", encoding="utf-8").write(json.dumps({
        "id": batch.id, "arm": args.arm, "effort": args.effort,
        "n": len(requests), "est_usd": round(usd, 2),
        "submitted": datetime.now(timezone.utc).isoformat()}) + "\n")
    print(f"submitted {batch.id} ({batch.processing_status})")


def _batches() -> list[dict]:
    path = OUT / "batches.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _spent_so_far() -> float:
    path = OUT / "usage.json"
    if path.exists():
        return float(json.loads(path.read_text())["usd"])
    return sum(b.get("est_usd", 0.0) for b in _batches())


def stage_collect(args) -> None:
    api = client()
    usd = 0.0
    for meta in _batches():
        batch = api.messages.batches.retrieve(meta["id"])
        print(f"{meta['id']} {meta['arm']:6s} {batch.processing_status} "
              f"{batch.request_counts}")
        if batch.processing_status != "ended":
            continue
        path = OUT / f"results_{meta['arm']}.jsonl"
        seen = _completed_ids()
        with path.open("a", encoding="utf-8") as fh:
            for result in api.messages.batches.results(meta["id"]):
                if result.result.type != "succeeded":
                    continue
                if result.custom_id in seen:
                    continue
                msg = result.result.message
                text = next((b.text for b in msg.content if b.type == "text"), None)
                try:
                    verdict = json.loads(text)
                except (TypeError, ValueError):
                    continue
                u = msg.usage
                usd += (u.input_tokens / 1e6 * PRICE_IN
                        + u.output_tokens / 1e6 * PRICE_OUT)
                fh.write(json.dumps({
                    "custom_id": result.custom_id, "arm": meta["arm"],
                    "effort": meta["effort"], "verdict": verdict,
                    "in": u.input_tokens, "out": u.output_tokens}) + "\n")
    prior = 0.0
    if (OUT / "usage.json").exists():
        prior = float(json.loads((OUT / "usage.json").read_text())["usd"])
    (OUT / "usage.json").write_text(json.dumps({"usd": round(prior + usd, 4)}))
    print(f"MEASURED spend this study: ${prior + usd:.2f}")


# ------------------------------------------------------------------ scoring

def _load_results() -> pd.DataFrame:
    rows = []
    for path in OUT.glob("results_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            arm, effort, symbol, ymd = r["custom_id"].split("_")
            day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            rows.append({"arm": arm, "symbol": symbol, "date": day,
                         "effort": effort, **r["verdict"],
                         "in": r.get("in"), "out": r.get("out")})
    return pd.DataFrame(rows)


def _gate(merged: pd.DataFrame) -> pd.Series:
    """The live gate: the verdict's direction must agree with the sign of the
    tape reaction. Everything else is vetoed."""
    return (((merged["direction"] == "bullish") & (merged["move_pct"] > 0))
            | ((merged["direction"] == "bearish") & (merged["move_pct"] < 0)))


def _permutation_p(merged: pd.DataFrame, rng) -> float:
    """Null: verdicts carry no event-specific information. Shuffle the
    direction labels WITHIN month (preserving the monthly mix of verdicts
    and the monthly return regime), re-gate, and compare the spread."""
    obs = _spread(merged)
    null = np.empty(N_PERM)
    groups = [g.index.to_numpy() for _, g in merged.groupby("month")]
    directions = merged["direction"].to_numpy()
    work = merged.copy()
    for i in range(N_PERM):
        shuffled = directions.copy()
        for idx in groups:
            pos = merged.index.get_indexer(idx)
            shuffled[pos] = rng.permutation(shuffled[pos])
        work["direction"] = shuffled
        null[i] = _spread(work)
    return float((null >= obs).mean())


def _spread(merged: pd.DataFrame) -> float:
    keep = _gate(merged)
    if keep.sum() == 0 or (~keep).sum() == 0:
        return 0.0
    return merged.loc[keep, "mech_pnl"].mean() - merged.loc[~keep, "mech_pnl"].mean()


def stage_score(args) -> None:
    universe = load_universe()
    universe["date"] = universe["date"].astype(str)
    results = _load_results()
    if results.empty:
        raise SystemExit("no results yet")
    rng = np.random.default_rng(20260806)
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    merged_all = results.merge(universe, on=["symbol", "date"], how="inner")
    merged_all["mech_pnl"] = (np.sign(merged_all["move_pct"])
                              * merged_all["fwd_bp"] - COSTS_BP)
    merged_all["age_days"] = (
        pd.Timestamp(date.today()) - pd.to_datetime(merged_all["date"])).dt.days

    emit(f"# LLM gate, 5 years, {MODEL} — contamination-controlled")
    emit("")
    emit(f"universe: {len(universe)} gate-{GATE:.0f}% events, top-{TOP_PER_DAY}"
         f"/day above ${MIN_DV/1e6:.0f}M, {universe['date'].min()}"
         f"..{universe['date'].max()}")
    emit(f"costs {COSTS_BP:.0f}bp round trip; mechanical baseline is the same "
         f"trades ungated")
    emit("")

    for arm, g in merged_all.groupby("arm"):
        if arm == "notext":
            continue
        emit(f"## arm: {arm}  (n={len(g)})")
        keep = _gate(g)
        emit(f"mechanical {g['mech_pnl'].mean():+.1f}bp  |  "
             f"LLM-gated n={int(keep.sum())} {g.loc[keep, 'mech_pnl'].mean():+.1f}bp"
             f"  |  vetoed n={int((~keep).sum())} "
             f"{g.loc[~keep, 'mech_pnl'].mean():+.1f}bp  |  spread "
             f"{_spread(g):+.1f}bp")
        emit(f"permutation null (shuffle verdicts within month, "
             f"{N_PERM}x): p={_permutation_p(g, rng):.3f}")
        emit("")
        emit("| year | n | mech | gated | vetoed | spread |")
        emit("|---|---|---|---|---|---|")
        for year, yg in g.groupby("year"):
            k = _gate(yg)
            emit(f"| {year} | {len(yg)} | {yg['mech_pnl'].mean():+.1f} | "
                 f"{yg.loc[k, 'mech_pnl'].mean():+.1f} | "
                 f"{yg.loc[~k, 'mech_pnl'].mean():+.1f} | "
                 f"{_spread(yg):+.1f} |")
        # Contamination gradient: does the edge grow with how long the event
        # has been sitting in the corpus?
        gated = g[_gate(g)]
        if len(gated) > 30:
            x = gated["age_days"].to_numpy(float)
            y = gated["mech_pnl"].to_numpy(float)
            slope, intercept = np.polyfit(x, y, 1)
            resid = y - (slope * x + intercept)
            se = float(np.sqrt((resid @ resid) / (len(x) - 2)
                               / ((x - x.mean()) @ (x - x.mean()))))
            emit("")
            emit(f"contamination gradient: gated P&L vs event age = "
                 f"{slope * 365:+.1f}bp/year (SE {se * 365:.1f}). A memorised "
                 f"edge should RISE with age; flat is evidence against it.")
        if arm == "blind" and "guessed_company" in g:
            hit = g.apply(lambda r: str(r["symbol"]).upper()
                          in str(r.get("guessed_company", "")).upper(), axis=1)
            named = g["identification_confidence"].isin(["medium", "high"])
            emit("")
            emit(f"re-identification leak: named the right ticker on "
                 f"{hit.mean() * 100:.1f}% of samples; claimed medium/high "
                 f"identification confidence on {named.mean() * 100:.1f}%.")
        emit("")

    notext = merged_all[merged_all["arm"] == "notext"]
    if not notext.empty:
        emit(f"## memory probe (no release text, n={len(notext)})")
        recalled = notext[notext["recalls_event"] == "yes"]
        guessed = notext[notext["recalled_next_session_direction"] != "unknown"]
        if len(guessed):
            actual_up = guessed["fwd_bp"] > 0
            said_up = guessed["recalled_next_session_direction"] == "up"
            acc = float((actual_up == said_up).mean())
            emit(f"claimed recall on {len(recalled)}/{len(notext)} events; "
                 f"gave a direction on {len(guessed)}.")
            emit(f"outcome-recall accuracy: {acc * 100:.1f}% vs 50% chance "
                 f"(n={len(guessed)}, binomial SE "
                 f"{np.sqrt(0.25 / len(guessed)) * 100:.1f}pp).")
        k = _gate(notext)
        emit(f"gate on memory alone: n={int(k.sum())} "
             f"{notext.loc[k, 'mech_pnl'].mean():+.1f}bp vs mechanical "
             f"{notext['mech_pnl'].mean():+.1f}bp -> spread "
             f"{_spread(notext):+.1f}bp.")
        emit("This is the upper bound on what memorisation can be worth: the "
             "prompt contains no information.")
        emit("")

    spent = _spent_so_far()
    emit(f"measured API spend: ${spent:.2f}")
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
           / f"llm_contamination_{stamp}.md")
    doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {doc}")


def stage_scrub(args) -> None:
    """Audit the anonymiser BEFORE spending anything on the blind arm: how
    much identity survives the scrub, measured over every cached release.
    A scrub nobody checked is a control nobody has."""
    with httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True) as http:
        _, names = ticker_map(http)
    rows = []
    for path in sorted(TEXTS.glob("*.txt")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        if len(raw) < 500:
            continue
        symbol = path.name.split("_")[0]
        company = names.get(symbol, "")
        out, subs = anonymise(raw, symbol, company)
        tokens = [t for t in re.split(r"[^A-Za-z0-9']+", company)
                  if len(t) > 3 and t.upper() not in _STOP]
        rows.append({
            "symbol": symbol, "subs": subs,
            "ticker_left": len(re.findall(
                rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", out, re.I)),
            "year_left": len(re.findall(r"\b(?:19|20)\d{2}\b", out)),
            "name_left": sum(len(re.findall(rf"\b{re.escape(t)}\b", out, re.I))
                             for t in tokens),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("no cached texts to audit — run the `texts` stage first")
    print(f"\n=== SCRUB AUDIT over {len(df)} cached releases ===")
    print(f"substitutions per release: median {df['subs'].median():.0f}, "
          f"min {df['subs'].min()}")
    for col, label in (("ticker_left", "ticker"), ("name_left", "company name"),
                       ("year_left", "year")):
        clean = (df[col] == 0).mean() * 100
        print(f"{label:>13} fully removed on {clean:5.1f}% of releases "
              f"(mean residual {df[col].mean():.2f})")
    print("\nResidual identity is expected and is NOT assumed to be zero — a "
          "large issuer is often identifiable from its own numbers. The blind "
          "arm measures that directly via guessed_company; this audit only "
          "proves the mechanical scrub is doing its job.")


def stage_calibrate(args) -> None:
    """Effort A/B on the most recent slice, where a fable result already
    exists to compare against. Run this BEFORE spending the 5-year budget:
    if low matches medium here, the whole study runs at low."""
    universe = load_universe()
    universe = universe[universe["date"].astype(str) >= args.since]
    print(f"calibration slice: {len(universe)} events since {args.since}")
    plans = []
    total = 0.0
    for effort in ("low", "medium"):
        requests = _requests_for("named", universe, effort, args.limit)
        usd, tok = estimate_cost(requests, effort)
        total += usd
        plans.append((effort, requests, usd))
        print(f"  effort={effort}: {len(requests)} requests, "
              f"~{tok / 1e6:.2f}M in, EST ${usd:.2f}")
    spent = _spent_so_far()
    print(f"calibration total EST ${total:.2f}; already spent ${spent:.2f}; "
          f"budget ${args.budget:.2f}")
    if spent + total > args.budget:
        raise SystemExit("REFUSED: calibration alone would exceed the budget.")
    if args.dry_run:
        print("dry run — nothing submitted")
        return
    api = client()
    OUT.mkdir(parents=True, exist_ok=True)
    for effort, requests, usd in plans:
        if not requests:
            continue
        batch = api.messages.batches.create(requests=requests)
        (OUT / "batches.jsonl").open("a", encoding="utf-8").write(json.dumps({
            "id": batch.id, "arm": "named", "effort": effort,
            "n": len(requests), "est_usd": round(usd, 2),
            "submitted": datetime.now(timezone.utc).isoformat()}) + "\n")
        print(f"submitted {batch.id} effort={effort} n={len(requests)}")
    print("poll with `collect`, then `score` and compare the two efforts "
          "before committing the 5-year budget.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["texts", "scrub", "calibrate", "submit",
                                      "collect", "score"])
    ap.add_argument("--arm", default="named", choices=["named", "blind", "notext"])
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--window", default="", help="comma-separated years")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since", default="2026-02-01")
    ap.add_argument("--budget", type=float, default=50.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    {"texts": stage_texts, "scrub": stage_scrub, "calibrate": stage_calibrate,
     "submit": stage_submit, "collect": stage_collect,
     "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
