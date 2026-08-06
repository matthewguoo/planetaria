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
# MEASURED on the 359-request calibration (2026-08-06), not guessed: input
# runs ~5,120 tok for a 12k-char release (3.6 chars/token underestimated it,
# so the char model below is corrected too), and output including thinking
# is 453 tok at low, 797 at medium.
EST_OUT_TOKENS = {"low": 460, "medium": 800, "high": 1600}
CHARS_PER_TOKEN = 2.45


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


# ------------------------------------------------------- acceptance timing

SUBS = CACHE / "sec_submissions"


def acceptance_et(raw: str) -> datetime | None:
    """EDGAR's acceptanceDateTime, in ET.

    The value is a real UTC instant with a real `Z`, contrary to the note that
    stood in research_leadup_account_sim until 2026-08-06 ("ET-mislabeled-Z").
    Two facts settle it, both reproducible from the cached submissions: the
    median post-15:00 acceptance shifts 58 minutes between Apr-Oct and Nov-Feb
    (a DST boundary, which issuer behaviour cannot produce), and read as ET the
    histogram of 68,394 cached 8-Ks puts filings at midnight and 02:00, when
    EDGAR is shut.
    """
    try:
        return (datetime.fromisoformat(raw.replace("Z", "+00:00"))
                .astimezone(ET))
    except Exception:
        return None


def submissions(cik: int, http: httpx.Client) -> list[dict]:
    """Every 8-K in a filer's history, cached to disk. Walks the older shards
    as well as `filings.recent`, which holds only ~1000 filings and for a
    prolific filer does not reach back ten years."""
    SUBS.mkdir(parents=True, exist_ok=True)
    cache = SUBS / f"CIK{cik:010d}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))["rows"]
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
        acc_dt = shard.get("acceptanceDateTime") or [""] * len(shard["form"])
        for i in range(len(shard["form"])):
            if not shard["form"][i].startswith("8-K"):
                continue
            rows.append({"form": shard["form"][i],
                         "filingDate": shard["filingDate"][i],
                         "acceptance": acc_dt[i], "items": items[i] or "",
                         "acc": shard["accessionNumber"][i]})
    cache.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return rows


PROVENANCE = CACHE / "lookahead_provenance.parquet"
ENTRY_MIN = 16 * 60 + 20    # the entry print, ET
CLOSE_MIN = 16 * 60         # the closing bell, ET


def timing_ok(df: pd.DataFrame) -> pd.Series:
    """True where the 8-K behind the event was accepted by EDGAR between the
    16:00 bell and the 16:20 entry print.

    FOUND 2026-08-06 by verify_no_lookahead.py, and it invalidates part of
    every panel built before that date. `fetch_calendar_window` classifies a
    filing's hour by slicing acceptanceDateTime as text and comparing to
    16:00, on the belief — written into its docstring — that the timestamp is
    ET wearing a spurious `Z`. It is not: it is real UTC. Two things prove it,
    both from the cached submissions: the median post-15:00 acceptance moves
    58 minutes between Apr-Oct and Nov-Feb (a DST shift, which issuer
    behaviour cannot produce), and read as ET the histogram puts filings at
    midnight and 02:00 when EDGAR is shut.

    So the calendar's "amc" bucket really means "after 16:00 UTC" — after
    noon ET in summer. Of the 1,552 scored events, 75 (4.8%) were accepted
    BEFORE the close and are not after-hours releases at all, and 152 (9.8%)
    were accepted AFTER 16:20, so the release was not public at the price the
    study enters at. Both are excluded by this filter. The remaining 1,325
    are clean, and score BETTER than the full set (+210.3bp gated vs +185.7),
    so the mis-timed events were diluting the result rather than creating it.

    What this filter does NOT repair: selection. The per-day top-5 was ranked
    over a candidate pool that included the mis-timed names, so on affected
    days the shipped watchlist is not the one a corrected calendar would
    produce. Fixing that needs the EDGAR calendars rebuilt, which changes the
    universe and would require re-running the study.
    """
    if not PROVENANCE.exists():
        raise SystemExit(
            "no lookahead_provenance.parquet — run "
            "`python scripts/verify_no_lookahead.py --only provenance` first "
            "(it fetches EDGAR acceptance times; free, ~3 min cold)")
    prov = pd.read_parquet(PROVENANCE)
    prov = prov[prov["status"] == "ok"][["symbol", "date", "acc_min"]]
    key = df["symbol"].astype(str) + "|" + df["date"].astype(str)
    lookup = dict(zip(prov["symbol"] + "|" + prov["date"], prov["acc_min"]))
    mins = key.map(lookup)
    return (mins >= CLOSE_MIN) & (mins <= ENTRY_MIN)


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


def fetch_release_text(sym: str, cik: int, day: str, http: httpx.Client,
                       acc: str | None = None) -> str | None:
    """8-K EX-99 text for the event date (the live feed's own resolution
    logic). Cached; an empty cache file means 'looked, found nothing' and is
    not retried.

    Pass `acc` when the caller already knows the accession — the v2 calendar
    stores it per event. That is not just a speed-up: the fallback below
    matches on EDGAR's `filingDate`, and for anything accepted after 17:30 ET
    the filing date is the NEXT business day, so a date-keyed lookup silently
    finds nothing for exactly the late filers the acceptance-relative panel
    exists to recover.
    """
    TEXTS.mkdir(parents=True, exist_ok=True)
    cache = TEXTS / f"{sym}_{day}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace") or None
    if acc is None:
        acc = next((r["acc"] for r in submissions(cik, http)
                    if r["form"] == "8-K" and "2.02" in r["items"]
                    and r["filingDate"] == day), None)
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
    # Named IR/press contacts are a strong identity signal — "Investor Contact:
    # Ahmed Pasha" is one search away from the issuer — and they survived the
    # company-name pass because a person's name is not the company's. Bounded
    # to two capitalised words after the label so it cannot run away.
    text = sub(r"(?:Investor|Media|Press|Analyst|Corporate)\s+"
               r"(?:Relations\s+)?Contacts?\s*:?\s*"
               r"(?:[A-Z][A-Za-z.'-]+\s+){1,3}", "Contact: [name] ", text)
    # US numbers are usually written bare (703-682-6451), and the old pattern
    # required a 1-2 digit country code first, so it matched almost nothing.
    # At least one separator between groups keeps it off financial tables.
    text = sub(r"\+?\d{1,2}[\s.\-(]*\d{3}[\s.\-)]*\d{3}[\s.\-]*\d{4}\b", "[phone]", text)
    text = sub(r"\(?\b\d{3}\)?[\s.\-]{1,2}\d{3}[\s.\-]{1,2}\d{4}\b", "[phone]", text)
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
    tok_in = chars / CHARS_PER_TOKEN
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

def universe_for(args) -> pd.DataFrame:
    """The event set a stage operates on.

    v2 is the acceptance-relative panel (research_event_panel) and is the
    default from 2026-08-06: its reaction is measured 15 minutes after each
    event's OWN EDGAR acceptance, so the mis-timed events the v1 panel had to
    discard are measured properly instead. v1 stays reachable because the
    paper reports what the repair changed.
    """
    if getattr(args, "panel", "v2") == "v2":
        from research_event_panel import load_universe_v2
        u = load_universe_v2(getattr(args, "windows", "ten"))
        return u.rename(columns={"edate": "_edate"})
    return load_universe()


def stage_texts(args) -> None:
    universe = universe_for(args)
    print(f"{len(universe)} selected events "
          f"{universe['date'].min()}..{universe['date'].max()}")
    # The v2 calendar already resolved which accession each event came from,
    # so hand it straight to the fetcher instead of re-deriving it from a
    # filing DATE that is wrong for every filer past 17:30 ET.
    acc_of: dict[tuple[str, str], str] = {}
    if getattr(args, "panel", "v2") == "v2":
        from research_event_panel import cal_path, windows
        for lo, hi in windows(getattr(args, "windows", "ten")):
            if cal_path(lo, hi).exists():
                cal = pd.read_parquet(cal_path(lo, hi))
                acc_of.update({(s, d): a for s, d, a
                               in zip(cal["symbol"], cal["date"], cal["acc"])})
        print(f"{len(acc_of)} accessions available from the v2 calendars")
    with httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True) as http:
        cik, _ = ticker_map(http)
        have = miss = 0
        for n, (_, row) in enumerate(universe.iterrows()):
            c = cik.get(row["symbol"])
            if c is None:
                miss += 1
                continue
            day = str(row["date"])
            cached = (TEXTS / f"{row['symbol']}_{day}.txt").exists()
            text = fetch_release_text(row["symbol"], c, day, http,
                                      acc_of.get((row["symbol"], day)))
            have += bool(text)
            miss += not text
            if n % 100 == 0:
                print(f"  texts {n}/{len(universe)} have={have} miss={miss}")
            if not cached:
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


def stratified_sample(universe: pd.DataFrame, n: int,
                      seed: int = 20260806) -> pd.DataFrame:
    """A year-stratified random subset, DETERMINISTIC in (n, seed).

    The contamination arms cost real money, so they run on a sample rather
    than the full panel — but blind and notext must land on the SAME events
    or the three-arm comparison is three different studies. Same n, same seed,
    same universe gives the same rows every time, which is why this is a pure
    function rather than a flag that shuffles per invocation.
    """
    if n <= 0 or len(universe) <= n:
        return universe
    rng = np.random.default_rng(seed)
    years = sorted(universe["year"].unique())
    per = max(1, n // len(years))
    keep = []
    for year in years:
        g = universe[universe["year"] == year]
        take = min(len(g), per)
        keep.append(g.iloc[sorted(rng.choice(len(g), take, replace=False))])
    return pd.concat(keep).sort_values(["date", "symbol"])


def stage_submit(args) -> None:
    universe = universe_for(args)
    if args.window:
        universe = universe[universe["year"].isin(args.window.split(","))]
    if args.sample:
        before = len(universe)
        universe = stratified_sample(universe, args.sample, args.seed)
        print(f"stratified sample: {len(universe)} of {before} events "
              f"(seed {args.seed}, year-balanced)")
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
    api = client()
    batch = api.messages.batches.create(requests=requests)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "batches.jsonl").open("a", encoding="utf-8").write(json.dumps({
        "id": batch.id, "arm": args.arm, "effort": args.effort,
        "n": len(requests), "est_usd": round(usd, 2),
        "submitted": datetime.now(timezone.utc).isoformat()}) + "\n")
    print(f"submitted {batch.id} ({batch.processing_status})")
    write_status(api, args.budget)


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
    # ONE effort across all years. The calibration deliberately ran the 2026
    # slice at two efforts; mixing them would confound the per-year edge and
    # the contamination gradient with an effort change at the 2026 boundary.
    results = results[results["effort"] == args.effort]
    if results.empty:
        raise SystemExit(f"no results at effort={args.effort}")
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
        gated = g[keep]
        t = (gated["mech_pnl"].mean()
             / (gated["mech_pnl"].std(ddof=1) / np.sqrt(len(gated))))
        emit(f"gated t={t:.2f} · keeps {keep.mean() * 100:.0f}% of events · "
             f"win rate {(gated['mech_pnl'] > 0).mean() * 100:.1f}% vs "
             f"{(g['mech_pnl'] > 0).mean() * 100:.1f}% ungated")
        sides = []
        for side, label in ((1, "long"), (-1, "short")):
            ss = gated[np.sign(gated["move_pct"]) == side]
            if len(ss):
                sides.append(f"{label} n={len(ss)} {ss['mech_pnl'].mean():+.1f}bp")
        emit("by side: " + " · ".join(sides))
        emit("verdict mix: " + ", ".join(
            f"{k} {v:.0%}" for k, v in
            g["direction"].value_counts(normalize=True).items()))
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

    emit("## what this does NOT control for")
    emit("")
    emit("- The `blind` and `notext` arms are the DIRECT contamination "
         "controls. Until they run, the evidence against memorisation is "
         "indirect: the age gradient and the relative strength of the "
         "least-contaminated year.")
    emit("- Regime priors survive any scrub. A model trained on text written "
         "after these events knows what kind of company this is, even "
         "recalling no specific release.")
    emit("- Survivorship: the universe is built from today's SEC ticker map, "
         "so issuers fully delisted since are absent. This flatters the bear "
         "regime most.")
    emit("- Costs are a flat "
         f"{COSTS_BP:.0f}bp round trip, not a per-name spread model, and the "
         "entry assumes a fill AT the reaction print. The live SIP problem "
         "says that fill is the part still unproven.")
    emit("- Consensus reads 'unknown' in every prompt. Live has real "
         "consensus for ~2/3 of names, so this understates the information "
         "the live strategy actually gets.")
    emit("- Exits are the panel's next-session 15:55 close. Intraday TP/SL "
         "brackets are not walked anywhere in this study.")
    emit("")
    spent = _spent_so_far()
    emit(f"measured API spend: ${spent:.2f}")
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
           / f"llm_contamination_{stamp}.md")
    doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {doc}")


def _identified(guess: str, symbol: str, company: str) -> bool:
    """Did the blind arm work out who it was reading?

    Counted as identified if the guess contains the ticker as a standalone
    token, or any distinctive word from the registered company name. Generous
    on purpose: this number is the leak gauge, and a leak gauge that flatters
    the scrub is worthless.
    """
    g = str(guess or "").upper()
    if not g or g in ("UNKNOWN", "N/A", "NONE", ""):
        return False
    if re.search(rf"(?<![A-Z0-9]){re.escape(symbol.upper())}(?![A-Z0-9])", g):
        return True
    for token in re.split(r"[^A-Za-z0-9']+", str(company or "")):
        if len(token) > 3 and token.upper() not in _STOP and token.upper() in g:
            return True
    return False


def compute_arms(args) -> dict:
    """The two direct contamination controls, scored against the live arm.

    THE QUESTION. `named` cannot separate reading from remembering: the model
    is told the ticker and four of the five years are inside its corpus. The
    two arms here separate them by removing one input at a time.

      blind   the same release with identity and time scrubbed out. If the
              edge survives here it came from reading the numbers, because
              there is nothing left to remember them BY. The arm also reports
              which company it thinks it is reading, so the scrub's failure
              rate is measured rather than assumed — and the edge can be
              re-scored on just the events it could NOT identify, which is
              the cleanest reading-not-remembering estimate available.
      notext  ticker and date, no release. There is no information in this
              prompt, so any edge is pure recall. It is the direct upper
              bound on what memorisation can be worth.
    """
    from research_holding_period import paths_file, resolve

    universe = universe_for(args)
    res = _load_results()
    res = res[res["effort"] == args.effort]
    if res.empty:
        raise SystemExit(f"no results at effort={args.effort}")
    with httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True) as http:
        _, names = ticker_map(http)

    paths = pd.read_parquet(paths_file("v2" if args.panel == "v2" else "v1"))
    lines: list[str] = []
    data: dict = {"effort": args.effort, "panel": args.panel}

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    arms = {}
    for arm, g in res.groupby("arm"):
        m = g.merge(universe, on=["symbol", "date"], how="inner")
        if not m.empty:
            arms[arm] = m
    if "named" not in arms:
        raise SystemExit("no named results on this panel")

    # Score every arm on ONE shared event set, resolved through the same
    # per-session exit machinery — otherwise an arm that happens to cover
    # easier events looks better for a reason that has nothing to do with
    # what it was given.
    shared = set(zip(arms["named"]["symbol"], arms["named"]["date"]))
    for arm, m in arms.items():
        shared &= set(zip(m["symbol"], m["date"]))
    emit(f"# Contamination arms — {MODEL}, effort {args.effort}, panel "
         f"{args.panel}")
    emit()
    emit("arms present: " + ", ".join(f"{a} n={len(m)}" for a, m in arms.items()))
    emit(f"paired subset scored below: {len(shared)} events covered by every arm")
    emit()
    data["paired_n"] = len(shared)
    data["arm_n"] = {a: len(m) for a, m in arms.items()}

    def scored(m: pd.DataFrame) -> pd.DataFrame:
        m = m[[(s, d) in shared for s, d in zip(m["symbol"], m["date"])]].copy()
        p = paths.drop(columns=["side"], errors="ignore").merge(
            m[["symbol", "date", "move_pct", "direction", "confidence"]],
            on=["symbol", "date"], how="inner")
        p["side"] = np.sign(p["move_pct"]).astype(int)
        ret, _ = resolve(p, np.ones(len(p), int), None, None)
        p["ret_bp"] = ret * 1e4
        p["gated"] = _gate(p)
        return p

    emit("| arm | n | ungated | gated | vetoed | spread | keeps |")
    emit("|---|---|---|---|---|---|---|")
    per_arm, data["per_arm"] = {}, []
    for arm in ("named", "blind", "notext"):
        if arm not in arms:
            continue
        p = scored(arms[arm])
        per_arm[arm] = p
        k = p["gated"]
        g, v = p.loc[k, "ret_bp"].mean(), p.loc[~k, "ret_bp"].mean()
        t = (p.loc[k, "ret_bp"].mean()
             / (p.loc[k, "ret_bp"].std(ddof=1) / np.sqrt(max(int(k.sum()), 1))))
        data["per_arm"].append({
            "arm": arm, "n": len(p), "gated_n": int(k.sum()),
            "mech": round(float(p["ret_bp"].mean()), 1),
            "gated": round(float(g), 1), "vetoed": round(float(v), 1),
            "spread": round(float(g - v), 1), "keeps": round(float(k.mean()) * 100, 1),
            "t": round(float(t), 2)})
        emit(f"| {arm} | {len(p)} | {p['ret_bp'].mean():+.1f} | {g:+.1f} | "
             f"{v:+.1f} | {g - v:+.1f} | {k.mean() * 100:.0f}% |")
    emit()

    if "blind" in arms:
        b = arms["blind"]
        b = b[[(s, d) in shared for s, d in zip(b["symbol"], b["date"])]].copy()
        b["ident"] = [
            _identified(gc, sym, names.get(sym, ""))
            for gc, sym in zip(b.get("guessed_company", ""), b["symbol"])]
        emit("## Identity ablation — how leaky is the scrub?")
        emit()
        emit(f"named the right issuer on {b['ident'].mean() * 100:.1f}% of "
             f"{len(b)} scrubbed releases.")
        data["blind"] = {"n": len(b),
                         "ident_pct": round(float(b["ident"].mean()) * 100, 1),
                         "by_conf": [], "subsets": []}
        emit()
        emit("| self-reported identification confidence | n | actually right |")
        emit("|---|---|---|")
        for conf in ("none", "low", "medium", "high"):
            g = b[b["identification_confidence"] == conf]
            if len(g):
                emit(f"| {conf} | {len(g)} | {g['ident'].mean() * 100:.1f}% |")
                data["blind"]["by_conf"].append({
                    "conf": conf, "n": len(g),
                    "right_pct": round(float(g["ident"].mean()) * 100, 1)})
        emit()
        # THE MEASUREMENT THAT MATTERS. Re-score the gate on only the events
        # the model could not place. If the edge holds there, it is reading.
        p = per_arm["blind"]
        ident = dict(zip(zip(b["symbol"], b["date"]), b["ident"]))
        p = p.assign(ident=[ident.get((s, d), False)
                            for s, d in zip(p["symbol"], p["date"])])
        emit("| blind subset | n | gated | vetoed | spread |")
        emit("|---|---|---|---|---|")
        for label, sub in (("could NOT identify the issuer", p[~p["ident"]]),
                           ("identified the issuer", p[p["ident"]])):
            if not len(sub):
                continue
            k = sub["gated"]
            if k.sum() == 0 or (~k).sum() == 0:
                continue
            gg, vv = sub.loc[k, "ret_bp"].mean(), sub.loc[~k, "ret_bp"].mean()
            emit(f"| {label} | {len(sub)} | {gg:+.1f} | {vv:+.1f} | "
                 f"{gg - vv:+.1f} |")
            data["blind"]["subsets"].append({
                "label": label, "n": len(sub), "gated_n": int(k.sum()),
                "gated": round(float(gg), 1), "vetoed": round(float(vv), 1),
                "spread": round(float(gg - vv), 1)})
        emit()
        # Does the blind model reach the same verdict as the named one?
        pair = arms["named"][["symbol", "date", "direction"]].merge(
            b[["symbol", "date", "direction"]], on=["symbol", "date"],
            suffixes=("_named", "_blind"))
        agree = float((pair["direction_named"] == pair["direction_blind"]).mean())
        data["blind"]["agree_pct"] = round(agree * 100, 1)
        data["blind"]["agree_n"] = len(pair)
        emit(f"named and blind agree on direction for {agree * 100:.1f}% "
             f"of {len(pair)} paired events.")
        emit()

    if "notext" in arms:
        nt = arms["notext"]
        nt = nt[[(s, d) in shared for s, d in zip(nt["symbol"], nt["date"])]].copy()
        p = per_arm["notext"]
        fwd = dict(zip(zip(paths["symbol"], paths["date"]),
                       [c[0] for c in paths["closes"]]))
        emit("## Memory probe — the upper bound on what recall can be worth")
        emit()
        recalled = nt[nt["recalls_event"] == "yes"]
        guessed = nt[nt["recalled_next_session_direction"] != "unknown"]
        data["notext"] = {
            "n": len(nt), "recall_n": len(recalled),
            "recall_pct": round(len(recalled) / max(len(nt), 1) * 100, 1),
            "guessed_n": len(guessed)}
        emit(f"claimed to recall the report on {len(recalled)}/{len(nt)} events "
             f"({len(recalled) / max(len(nt), 1) * 100:.1f}%); volunteered a "
             f"next-session direction on {len(guessed)}.")
        if len(guessed):
            up = []
            for _, r in guessed.iterrows():
                nxt = fwd.get((r["symbol"], r["date"]))
                if nxt is None:
                    continue
                up.append(((nxt / r["react"] - 1) > 0,
                           r["recalled_next_session_direction"] == "up"))
            if up:
                acc = float(np.mean([a == b for a, b in up]))
                se = np.sqrt(0.25 / len(up))
                z = (acc - 0.5) / se
                data["notext"].update(acc_pct=round(acc * 100, 1),
                                      acc_n=len(up),
                                      se_pp=round(float(se) * 100, 1),
                                      z=round(float(z), 2))
                emit(f"outcome-recall accuracy {acc * 100:.1f}% on {len(up)} "
                     f"volunteered directions, against 50% chance "
                     f"(binomial SE {se * 100:.1f}pp, z = {z:+.2f}).")
        k = p["gated"]
        if k.any() and (~k).any():
            emit(f"gate on memory alone: n={int(k.sum())} "
                 f"{p.loc[k, 'ret_bp'].mean():+.1f}bp vs vetoed "
                 f"{p.loc[~k, 'ret_bp'].mean():+.1f}bp -> spread "
                 f"{p.loc[k, 'ret_bp'].mean() - p.loc[~k, 'ret_bp'].mean():+.1f}bp.")
        emit("There is no information in this prompt. Whatever this spread is, "
             "it is what memorisation alone buys.")
        emit()

    emit(f"measured API spend for the whole study: ${_spent_so_far():.2f}")
    data["spend_usd"] = round(_spent_so_far(), 2)
    data["lines"] = lines
    return data


def stage_arms(args) -> None:
    """Render compute_arms() to a dated note. build_report_data reads the same
    dict, so the note and the paper cannot disagree about a number."""
    data = compute_arms(args)
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    doc = (Path(__file__).resolve().parents[2] / "docs" / "notes"
           / f"contamination_arms_{stamp}.md")
    doc.write_text("\n".join(data["lines"]) + "\n", encoding="utf-8")
    print(f"\nwrote {doc}")


STATUS_FILE = (Path(__file__).resolve().parents[2] / "frontend" / "public"
               / "study-status.json")


def write_status(api=None, budget: float = 50.0) -> dict:
    """Publish study progress as a STATIC asset the LAB deck can poll.

    Deliberately a file, not an API route: the study lives in scripts/ and
    docs/notes, and app/ must not grow a reader for research artefacts just
    to make them visible. vite serves public/ at the root, so the deck
    fetches /study-status.json and app/ never learns this exists.
    """
    batches = []
    for meta in _batches():
        row = {k: meta.get(k) for k in ("id", "arm", "effort", "n", "est_usd",
                                        "submitted")}
        row["status"] = "unknown"
        if api is not None:
            try:
                b = api.messages.batches.retrieve(meta["id"])
                counts = b.request_counts
                row.update(status=b.processing_status,
                           succeeded=counts.succeeded, errored=counts.errored,
                           processing=counts.processing)
            except Exception as exc:
                row["status"] = f"error: {str(exc)[:60]}"
        batches.append(row)
    done = sum(b.get("succeeded") or 0 for b in batches)
    total = sum(b.get("n") or 0 for b in batches)
    status = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "spent_usd": round(_spent_so_far(), 2),
        "budget_usd": budget,
        "requests_done": done,
        "requests_total": total,
        "results_rows": len(_completed_ids()),
        "batches": batches[::-1],
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def stage_watch(args) -> None:
    """Poll the Batch API and refresh the status file so the LAB deck shows
    the study progressing live. Exits when every batch has ended."""
    api = client()
    while True:
        status = write_status(api, args.budget)
        pending = [b for b in status["batches"] if b["status"] != "ended"]
        print(f"{status['requests_done']}/{status['requests_total']} done · "
              f"${status['spent_usd']:.2f} · {len(pending)} batch(es) running")
        if not pending:
            stage_collect(args)
            write_status(api, args.budget)
            print("all batches ended")
            return
        _time.sleep(args.poll_s)


CURVE_FILE = (Path(__file__).resolve().parents[2] / "frontend" / "public"
              / "study-curve.json")
PER_NAME_CAP = 0.20      # engine dial
GROSS_CAP = 1.00         # engine dial
RISK_PCT = 0.5           # % of equity at the stop
SL_MULT, SL_FLOOR, SL_CAP = 2.5, 0.05, 0.12


BENCHMARKS = ("SPY", "QQQ", "TQQQ")


def _spy_daily(start: date, end: date, symbol: str = "SPY") -> pd.DataFrame:
    """Daily closes for a benchmark, cached. Split/dividend adjusted from the
    same source as everything else, not from memory.

    QQQ is here because "is this just market exposure?" is not answered by
    SPY alone — earnings reactions concentrate in tech, so the NASDAQ-100 is
    the benchmark the strategy could plausibly be a proxy for. TQQQ (3x QQQ)
    is the leverage control: if the returns were levered beta, TQQQ is what
    they would look like, drawdown included."""
    cache = CACHE / f"bench_{symbol}_{start}_{end}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    out = api.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), ET),
        end=datetime.combine(end, datetime.min.time(), ET),
        feed="sip", adjustment=Adjustment.ALL, limit=None))
    bars = out.data[symbol]
    df = pd.DataFrame({"date": [b.timestamp.astimezone(ET).date() for b in bars],
                       "close": [float(b.close) for b in bars]})
    df.to_parquet(cache)
    return df


def _avg_abs_ret(symbol: str, day: date) -> float | None:
    """Trailing avg |daily return| over the 5 prior sessions — the input to
    the engine's vol-scaled stop. Prior sessions only."""
    panel = _AVG_CACHE.get(symbol)
    if panel is None:
        return None
    dates, closes = panel
    i = np.searchsorted(dates, day)
    if i < 6:
        return None
    window = closes[i - 6:i]
    rets = np.abs(np.diff(window) / window[:-1])
    return float(rets.mean()) if len(rets) else None


_AVG_CACHE: dict[str, tuple[list, np.ndarray]] = {}


def _load_avg_cache() -> None:
    for path in sorted(CACHE.glob("bars_ohlc_*.parquet")):
        bars = pd.read_parquet(path)
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        for sym, g in bars.sort_values("date").groupby("symbol"):
            prior = _AVG_CACHE.get(sym)
            dates = g["date"].to_list()
            closes = g["c"].to_numpy()
            if prior is None:
                _AVG_CACHE[sym] = (dates, closes)
            else:                       # windows overlap; keep the union
                merged = dict(zip(prior[0], prior[1]))
                merged.update(zip(dates, closes))
                keys = sorted(merged)
                _AVG_CACHE[sym] = (keys, np.array([merged[k] for k in keys]))


BRACKETS = CACHE / "brackets_gated.parquet"


def stage_brackets(args) -> None:
    """Walk the engine's ACTUAL brackets on per-event minute bars.

    Everything else in this study measures the raw round trip — enter at the
    reaction print, exit next session 15:55 — because that is what the
    cached event panel stores. The live strategy does not trade that way: it
    arms a stop at clamp(2.5 x avg|daily ret|, 5%, 12%) and a take-profit at
    2x that, and the exit enforcer can fire either one after hours. Ignoring
    them makes the drawdown look better than it is (a stop that never fires
    cannot hurt you) and the winners smaller than they are (a TP that never
    fires gives the gain back into the close).

    So: refetch minute bars for the gated events and resolve each one
    properly. Costs nothing — historical SIP minute data is free-tier-fine
    — just wall clock. Stop-first on same-bar touches, matching the account
    sims. Cached, so this runs once."""
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    universe = load_universe()
    universe = universe.rename(columns={"date": "edate"})
    universe["date"] = universe["edate"].astype(str)
    results = _load_results()
    results = results[(results["effort"] == args.effort)
                      & (results["arm"] == "named")]
    m = results.merge(universe, on=["symbol", "date"], how="inner")
    m = m[_gate(m)].copy()
    _load_avg_cache()

    have: set[tuple[str, str]] = set()
    if BRACKETS.exists():
        prior = pd.read_parquet(BRACKETS)
        have = set(zip(prior["symbol"], prior["date"]))
    else:
        prior = pd.DataFrame()

    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows = []
    todo = [(i, r) for i, r in m.iterrows()
            if (r["symbol"], r["date"]) not in have]
    print(f"{len(todo)} of {len(m)} gated events need minute bars")
    for n, (_, row) in enumerate(todo):
        d = row["edate"]
        side = 1 if row["move_pct"] > 0 else -1
        avg = _avg_abs_ret(row["symbol"], d)
        sl = min(max(SL_MULT * (avg if avg else SL_FLOOR / SL_MULT),
                     SL_FLOOR), SL_CAP)
        tp = 2 * sl
        entry = float(row["react"])
        start = datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET)
        try:
            out = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=row["symbol"], timeframe=TimeFrame.Minute,
                start=start, end=start + pd.Timedelta(days=4).to_pytimedelta(),
                feed="sip"))
            bars = out.data.get(row["symbol"]) or []
        except Exception as exc:
            print(f"  {row['symbol']} {d}: {str(exc)[:70]}")
            bars = []
        exit_px, reason = None, None
        tp_px = entry * (1 + side * tp)
        sl_px = entry * (1 - side * sl)
        for bar in bars:
            ts = bar.timestamp.astimezone(ET)
            if ts.date() == d and ts.hour * 60 + ts.minute < 16 * 60 + 20:
                continue          # before the reaction print we entered at
            if ts.date() > d and (ts.hour * 60 + ts.minute) > 15 * 60 + 55:
                break             # past the T+1 time stop
            hi, lo = float(bar.high), float(bar.low)
            hit_sl = lo <= sl_px if side > 0 else hi >= sl_px
            hit_tp = hi >= tp_px if side > 0 else lo <= tp_px
            if hit_sl:            # stop-first on same-bar ties
                exit_px, reason = sl_px, "sl"
                break
            if hit_tp:
                exit_px, reason = tp_px, "tp"
                break
        if exit_px is None:
            exit_px, reason = float(row["exit"]), "time_stop"
        rows.append({"symbol": row["symbol"], "date": row["date"],
                     "side": side, "entry": entry, "sl_pct": sl, "tp_pct": tp,
                     "exit_px": exit_px, "reason": reason,
                     "bars": len(bars)})
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}")
        _time.sleep(0.22)

    out = pd.concat([prior, pd.DataFrame(rows)], ignore_index=True) \
        if len(prior) else pd.DataFrame(rows)
    out.to_parquet(BRACKETS)
    mix = out["reason"].value_counts()
    print(f"\nresolved {len(out)} gated events: "
          + ", ".join(f"{k} {v} ({v / len(out):.0%})" for k, v in mix.items()))
    out["ret"] = out["side"] * (out["exit_px"] / out["entry"] - 1) - COSTS_BP / 1e4
    print(f"bracketed mean {out['ret'].mean() * 1e4:+.1f}bp  |  "
          f"win {(out['ret'] > 0).mean() * 100:.1f}%")
    print(f"wrote {BRACKETS}")


def _lever(rets, k: float) -> list[float]:
    """Daily-rebalanced k-times exposure — the mechanic real leveraged ETFs
    use, so the path (and its volatility decay) is honest rather than a naive
    k x terminal return."""
    eq, out = 1.0, []
    for r in rets:
        eq *= (1 + k * r)
        out.append(eq)
    return out


def _max_dd(curve) -> float:
    peak, mdd = 1.0, 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    return mdd


def _iso_return_leverage(bench_rets, target: float, hi: float = 10.0) -> dict:
    """The SMALLEST daily-rebalanced exposure k for which k-times the index
    ends where the strategy ends.

    Not a bisection: terminal wealth is NOT monotone in k. Volatility decay
    makes it hump-shaped — past some k the index's own drawdowns compound
    faster than its gains, and more leverage ends LOWER, then goes to zero.
    Bisecting that returns the ceiling and a fake 100% drawdown. So scan,
    take the peak, and report honestly when the target is unreachable at ANY
    leverage — which is itself the answer to 'why not just lever the index'.
    """
    best_k, best_terminal = 0.0, 1.0
    found = None
    k = 0.0
    while k <= hi:
        k = round(k + 0.01, 2)
        curve = _lever(bench_rets, k)
        terminal = curve[-1]
        if terminal > best_terminal:
            best_k, best_terminal = k, terminal
        if found is None and terminal >= target:
            found = (k, curve)
    if found is None:
        peak = _lever(bench_rets, best_k)
        return {"reachable": False, "best_k": best_k,
                "best_terminal": round(best_terminal, 3),
                "max_drawdown_pct": round(_max_dd(peak) * 100, 1)}
    k, curve = found
    rr = np.diff(np.asarray([1.0] + curve)) / np.asarray([1.0] + curve[:-1])
    return {"reachable": True, "k": k,
            "max_drawdown_pct": round(_max_dd(curve) * 100, 1),
            "vol_annual_pct": round(float(rr.std(ddof=1) * np.sqrt(252) * 100), 1),
            "terminal": round(curve[-1], 3)}


def stage_curve(args) -> None:
    """Account curve for the LLM-gated stream vs SPY buy-and-hold, plus the
    daily-return regression that turns 'it made money' into alpha and beta.

    Honest about what the data supports: entries are the reaction print and
    exits are the next session's 15:55 close, the same round trip the bp
    numbers measure. Intraday TP/SL brackets are NOT walked — that needs
    per-event minute bars, which are not cached for all 1,552 events — so
    this is the strategy WITHOUT its brackets. Sizing, caps and compounding
    are the engine's own.
    """
    universe = load_universe()
    # `edate` is the real date object; `date` stays the string both sides
    # join on, so the merge cannot produce date_x/date_y.
    universe = universe.rename(columns={"date": "edate"})
    universe["date"] = universe["edate"].astype(str)
    results = _load_results()
    results = results[(results["effort"] == args.effort)
                      & (results["arm"] == "named")]
    if results.empty:
        raise SystemExit(f"no named results at effort={args.effort}")
    m = results.merge(universe, on=["symbol", "date"], how="inner")
    m = m[_gate(m)].copy()
    m["side"] = np.sign(m["move_pct"])
    m["ret"] = m["side"] * (m["exit"] / m["react"] - 1) - COSTS_BP / 1e4
    # --brackets swaps the raw round trip for the engine's ACTUAL exit: the
    # vol-scaled stop, the 2x take-profit, or the T+1 time stop, whichever
    # the minute bars hit first (see the `brackets` stage). Two different
    # questions — raw measures whether the gate picks direction; bracketed
    # measures what the strategy AS CONFIGURED would have returned.
    if args.brackets:
        if not BRACKETS.exists():
            raise SystemExit("run the `brackets` stage first")
        br = pd.read_parquet(BRACKETS)
        br["bret"] = br["side"] * (br["exit_px"] / br["entry"] - 1) - COSTS_BP / 1e4
        m = m.merge(br[["symbol", "date", "bret", "reason"]],
                    on=["symbol", "date"], how="inner")
        m["ret"] = m["bret"]
        print(f"bracketed: {len(m)} trades · "
              + " · ".join(f"{k} {v}" for k, v in m["reason"].value_counts().items()))
    m = m.sort_values("edate")

    _load_avg_cache()
    m["sl_eff"] = [
        min(max(SL_MULT * (a or SL_FLOOR / SL_MULT), SL_FLOOR), SL_CAP)
        for a in (_avg_abs_ret(r["symbol"], r["edate"]) for _, r in m.iterrows())]
    conf = {"high": 1.5, "medium": 1.0, "low": 0.5}
    m["mult"] = [conf.get(c, 1.0) * (0.75 if f else 1.0)
                 * (0.5 if abs(mv) > 6 else 1.0)
                 for c, f, mv in zip(m["confidence"], m["quality_flags"],
                                     m["move_pct"])]

    # The session spine is SPY's own trading calendar, not the set of days
    # that happen to have earnings — otherwise the daily series has holes and
    # the regression is against a benchmark sampled on different days.
    lo, hi = min(m["edate"]), max(m["edate"])
    spy = _spy_daily(lo, hi + pd.Timedelta(days=7).to_pytimedelta())
    spy = spy.sort_values("date").reset_index(drop=True)
    sessions = spy["date"].to_list()
    index = {d: i for i, d in enumerate(sessions)}

    # A trade entered after the close on day d is EXITED at day d+1's close,
    # so its P&L belongs to session d+1 — the same session whose SPY return
    # it is being regressed against. Bucketing it on d (the announce date)
    # shifts the strategy one day ahead of the benchmark and drives the beta
    # estimate spuriously toward zero.
    m["exit_idx"] = [
        (index.get(d, -1) + 1) if index.get(d, -1) >= 0 else -1
        for d in m["edate"]]
    m = m[(m["exit_idx"] > 0) & (m["exit_idx"] < len(sessions))]

    curves: dict[str, list[float]] = {}
    daily: dict[str, list[float]] = {}
    for policy in ("vol", "flat"):
        equity = 1.0
        eq_by_day, ret_by_day = [], []
        by_exit = {i: g for i, g in m.groupby("exit_idx")}
        for i in range(len(sessions)):
            pnl = gross = 0.0
            for _, row in by_exit.get(i, pd.DataFrame()).iterrows():
                weight = (RISK_PCT / 100.0 / row["sl_eff"] * row["mult"]
                          if policy == "vol" else 0.10)
                weight = min(weight, PER_NAME_CAP)
                if gross + weight > GROSS_CAP:
                    weight = max(0.0, GROSS_CAP - gross)
                gross += weight
                pnl += weight * row["ret"]
            ret_by_day.append(pnl)
            equity *= (1 + pnl)
            eq_by_day.append(equity)
        curves[policy] = eq_by_day
        daily[policy] = ret_by_day

    # Every benchmark on the SPY session grid, forward-filled across any
    # missing print so the return series line up index-for-index.
    bench: dict[str, dict] = {}
    for sym in BENCHMARKS:
        b = _spy_daily(lo, hi + pd.Timedelta(days=7).to_pytimedelta(), sym)
        px = dict(zip(b["date"], b["close"]))
        series, last = [], None
        for d in sessions:
            last = px.get(d, last)
            series.append(last)
        if series[0] is None:
            continue
        curve = [v / series[0] for v in series]
        rets = [0.0] + [series[i] / series[i - 1] - 1 for i in range(1, len(series))]
        bench[sym] = {"curve": curve, "ret": rets}

    def _describe(curve, rets, years):
        peak, mdd = 1.0, 0.0
        for e in curve:
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak)
        r = np.asarray(rets)
        return {
            "total_return_pct": round((curve[-1] - 1) * 100, 1),
            "cagr_pct": round((curve[-1] ** (1 / years) - 1) * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 1),
            "sharpe": round(float(r.mean() / r.std(ddof=1) * np.sqrt(252)), 2),
            "days_traded": int((r != 0).sum()),
        }

    years = (sessions[-1] - sessions[0]).days / 365.25
    stats = {}
    for policy in ("vol", "flat"):
        r = np.array(daily[policy])
        s_ = _describe(curves[policy], r, years)
        # One single-factor regression PER benchmark. Alpha only means
        # something next to the exposure it is adjusted for, and "is this
        # just the index?" is a different question for SPY, QQQ and TQQQ.
        s_["vs"] = {}
        for sym, b in bench.items():
            beta, alpha_d = np.polyfit(np.asarray(b["ret"]), r, 1)
            corr = float(np.corrcoef(np.asarray(b["ret"]), r)[0, 1])
            s_["vs"][sym] = {"alpha_annual_pct": round(alpha_d * 252 * 100, 2),
                             "beta": round(float(beta), 3),
                             "corr": round(corr, 3)}
        s_["alpha_annual_pct"] = s_["vs"].get("SPY", {}).get("alpha_annual_pct", 0.0)
        s_["beta"] = s_["vs"].get("SPY", {}).get("beta", 0.0)
        s_["trades"] = int(len(m))
        # "Could I just have levered the index?" — answered with the receipt.
        s_["iso"] = {}
        for sym, b in bench.items():
            s_["iso"][sym] = _iso_return_leverage(b["ret"], curves[policy][-1])
        stats[policy] = s_
    for sym, b in bench.items():
        s_ = _describe(b["curve"], b["ret"], years)
        s_.update(alpha_annual_pct=0.0, beta=1.0 if sym == "SPY" else None,
                  trades=0, vs={})
        for other, ob in bench.items():
            beta, alpha_d = np.polyfit(np.asarray(ob["ret"]), np.asarray(b["ret"]), 1)
            s_["vs"][other] = {"alpha_annual_pct": round(alpha_d * 252 * 100, 2),
                               "beta": round(float(beta), 3),
                               "corr": round(float(np.corrcoef(
                                   np.asarray(ob["ret"]),
                                   np.asarray(b["ret"]))[0, 1]), 3)}
        s_["beta"] = s_["vs"]["SPY"]["beta"]
        stats[sym.lower()] = s_

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "model": MODEL, "effort": args.effort,
        "span": [str(sessions[0]), str(sessions[-1])],
        "note": ("entry at the reaction print, exit next session 15:55; "
                 "13bp round-trip costs; engine sizing and caps; intraday "
                 "TP/SL brackets NOT walked"),
        "dates": [str(d) for d in sessions],
        "series": {"vol": [round(v, 5) for v in curves["vol"]],
                   "flat": [round(v, 5) for v in curves["flat"]],
                   **{s.lower(): [round(v, 5) for v in b["curve"]]
                      for s, b in bench.items()}},
        "stats": stats,
    }
    CURVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    target = (CURVE_FILE.with_name("study-curve-bracketed.json")
              if args.brackets else CURVE_FILE)
    target.write_text(json.dumps(payload), encoding="utf-8")

    print(f"\n=== {len(m)} gated trades over {len(sessions)} sessions "
          f"{sessions[0]}..{sessions[-1]} ===")
    print(f"{'':6}{'total':>10}{'CAGR':>8}{'alpha':>9}{'beta':>7}"
          f"{'sharpe':>8}{'maxDD':>8}{'days in':>9}")
    for name in ("vol", "flat", "spy", "qqq", "tqqq"):
        st = stats.get(name)
        if not st:
            continue
        print(f"{name:6}{st['total_return_pct']:>9.1f}%{st['cagr_pct']:>7.2f}%"
              f"{st['alpha_annual_pct']:>8.2f}%"
              f"{(st['beta'] if st['beta'] is not None else 0):>7.3f}"
              f"{st['sharpe']:>8.2f}{st['max_drawdown_pct']:>7.1f}%"
              f"{st['days_traded']:>9d}")
    print()
    print("  alpha / beta / correlation of the strategy vs each index:")
    for policy in ("vol", "flat"):
        for sym, v in stats[policy]["vs"].items():
            print(f"    {policy:5s} vs {sym:5s}  alpha {v['alpha_annual_pct']:+7.2f}%/yr"
                  f"   beta {v['beta']:+.3f}   corr {v['corr']:+.3f}")
    print()
    print("  same terminal wealth from leverage alone — what it would have cost:")
    for policy in ("vol", "flat"):
        for sym, iso in stats[policy]["iso"].items():
            if iso["reachable"]:
                print(f"    {policy:5s} == {iso['k']:>5.2f}x {sym:5s}"
                      f"   maxDD {iso['max_drawdown_pct']:>6.1f}%"
                      f"   vol {iso['vol_annual_pct']:>5.1f}%/yr")
            else:
                print(f"    {policy:5s} vs {sym:5s}   UNREACHABLE at any "
                      f"leverage — best is {iso['best_k']:.2f}x ending at "
                      f"{iso['best_terminal']:.2f}x (maxDD "
                      f"{iso['max_drawdown_pct']:.1f}%)")
    print(f"\nwrote {CURVE_FILE}")


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
    universe = universe_for(args)
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
    write_status(api, args.budget)
    print("poll with `collect`, then `score` and compare the two efforts "
          "before committing the 5-year budget.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["texts", "scrub", "calibrate", "submit",
                                      "collect", "watch", "score", "curve",
                                      "brackets", "arms"])
    ap.add_argument("--arm", default="named", choices=["named", "blind", "notext"])
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--window", default="", help="comma-separated years")
    ap.add_argument("--panel", default="v2", choices=["v1", "v2"],
                    help="v2 = acceptance-relative reaction windows")
    ap.add_argument("--windows", default="ten", choices=["five", "ten"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sample", type=int, default=None,
                    help="year-stratified random subset; deterministic "
                         "in (sample, seed) so two arms share events")
    ap.add_argument("--seed", type=int, default=20260806)
    ap.add_argument("--since", default="2026-02-01")
    ap.add_argument("--budget", type=float, default=50.0)
    ap.add_argument("--poll-s", type=float, default=30.0)
    ap.add_argument("--brackets", action="store_true",
                    help="use the walked TP/SL/time-stop exits, not the raw "
                         "next-session close")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    {"texts": stage_texts, "scrub": stage_scrub, "calibrate": stage_calibrate,
     "submit": stage_submit, "collect": stage_collect, "watch": stage_watch,
     "score": stage_score, "curve": stage_curve, "brackets": stage_brackets,
     "arms": stage_arms}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
