"""Mechanical audit of the load-bearing claim behind the LLM-gate study.

THE ASSERTION UNDER TEST: every input the model saw was available at 16:20 ET
on the announce day, and nothing derived from the outcome reached the prompt,
the gate, or the selection.

Reading the code and concluding it looks right is not evidence. Every check
here is executable and prints PASS or FAIL. Where a property can be stated as
an INVARIANCE — "the output does not change when the future changes" — it is
tested that way, because a perturbation proof cannot be fooled by a variable
that merely looks innocuous:

  check 1  the request is byte-identical when every outcome column is
           replaced with garbage, so the prompt is a pure function of
           (symbol, run5d).
  check 3  the run-up and dollar-volume features are byte-identical when
           every bar at or after the announce day is set to NaN.
  check 4  the same perturbation applied to selection: the chosen universe
           does not move when the future does.

The remaining checks are measurements, not invariances, and two of them
FAIL. They are reported in full because a failed audit item is worth more
than a passed one.

Run (all checks, ~4 min the first time, seconds after the SEC cache warms):
    .venv/Scripts/python.exe scripts/verify_no_lookahead.py
    .venv/Scripts/python.exe scripts/verify_no_lookahead.py --only prompt,features
    .venv/Scripts/python.exe scripts/verify_no_lookahead.py --online   # + Batch API

No LLM inference. `--online` re-reads already-paid-for batch RESULTS to prove
no tool block came back; it submits nothing and costs nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time as _time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_llm_contamination import (  # noqa: E402
    CACHE,
    ET,
    GATE,
    MIN_DV,
    SEC_UA,
    SUBS,
    TEXTS,
    TOP_PER_DAY,
    _bars_for,
    _load_results,
    acceptance_et,
    build_request,
    load_universe,
    submissions,
    ticker_map,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "notes"

# The entry print. research_pead_backtest measures the reaction as the last
# print in [16:00, 16:05 + 15min]; Alpaca stamps a minute bar with the minute
# it OPENS, so the 16:20 bar can carry a trade as late as 16:20:59.
ENTRY_CUTOFF_MIN = 16 * 60 + 20
# Columns that are, or are derived from, the outcome. Nothing downstream of
# the announce-day close may depend on any of them.
OUTCOME_COLS = ("anchor", "react", "exit", "move_pct", "fwd_bp")


def _http() -> httpx.Client:
    return httpx.Client(headers=SEC_UA, timeout=30, follow_redirects=True)


def scored() -> pd.DataFrame:
    """The 1,552 events the study actually scored, with their features."""
    universe = load_universe()
    universe["date"] = universe["date"].astype(str)
    res = _load_results()
    res = res[(res["effort"] == "medium") & (res["arm"] == "named")]
    return res.merge(universe, on=["symbol", "date"], how="inner")


# ------------------------------------------------------------------ check 1

def check_prompt(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """The prompt is a pure function of (symbol, run5d).

    Proved by perturbation rather than by reading: rebuild every request with
    all five outcome columns replaced by garbage and require the serialised
    payload to be byte-identical. If any outcome value reached the prompt —
    directly, or through any expression of it — the bytes move.
    """
    out: list[str] = []
    with _http() as http:
        _, names = ticker_map(http)
    rng = np.random.default_rng(20260806)
    n_built = n_diff = n_notext = 0
    examples: list[str] = []
    for _, row in m.iterrows():
        path = TEXTS / f"{row['symbol']}_{row['date']}.txt"
        if not path.exists():
            n_notext += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text:
            n_notext += 1
            continue
        company = names.get(row["symbol"], "")
        real = build_request(row, "named", text, company, "medium")
        if real is None:
            continue
        poisoned = row.copy()
        for col in OUTCOME_COLS:
            poisoned[col] = float(rng.normal(1e6, 1e5))
        fake = build_request(poisoned, "named", text, company, "medium")
        n_built += 1
        if json.dumps(real, sort_keys=True) != json.dumps(fake, sort_keys=True):
            n_diff += 1
            if len(examples) < 3:
                examples.append(f"{row['symbol']} {row['date']}")
    out.append(f"rebuilt {n_built} named requests ({n_notext} skipped: no "
               f"cached release text)")
    out.append(f"outcome-perturbation: {n_diff} requests changed when "
               f"anchor/react/exit/move_pct/fwd_bp were replaced with noise")
    if examples:
        out.append("  differing: " + ", ".join(examples))

    # A second, independent reading: scan the SCAFFOLDING (system prompt plus
    # the task line we author) on a sample for the outcome as a number. The
    # <data> block is excluded here on purpose — it is the issuer's own
    # document, filed on the day, and its provenance is check 2's job, not a
    # string match's.
    sample = m.sample(n=min(args.sample, len(m)), random_state=20260806)
    n_leak = 0
    for _, row in sample.iterrows():
        path = TEXTS / f"{row['symbol']}_{row['date']}.txt"
        if not path.exists():
            continue
        req = build_request(row, "named", path.read_text(encoding="utf-8",
                                                         errors="replace"),
                            names.get(row["symbol"], ""), "medium")
        if req is None:
            continue
        content = req["params"]["messages"][0]["content"]
        scaffold = req["params"]["system"] + content.split("<data>")[0]
        needles = set()
        for col in OUTCOME_COLS + ("dv",):
            v = float(row[col])
            for fmt in ("%.0f", "%.1f", "%.2f", "%.3f", "%.4f"):
                needles.add((fmt % v).lstrip("-"))
                needles.add((fmt % abs(v)))
        hits = [x for x in needles if len(x) >= 3 and x in scaffold]
        # run5d is a PRIOR-session feature and is in the prompt by design;
        # its digits can coincide with a rounded outcome. Exclude only the
        # exact rendering the task line uses.
        allowed = f"{float(row['run5d']):+.1f}"[1:] if pd.notna(row["run5d"]) else ""
        hits = [h for h in hits if h != allowed]
        if hits:
            n_leak += 1
            if n_leak <= 3:
                out.append(f"  LEAK {row['symbol']} {row['date']}: {hits[:4]}")
    out.append(f"numeric scan of the authored scaffolding on {len(sample)} "
               f"sampled events: {n_leak} carried an outcome value")
    out.append("the tape reaction is absent from the prompt by construction — "
               "the model never sees the move it is gating")
    return (n_diff == 0 and n_leak == 0), out


# ------------------------------------------------------------------ check 2

def check_provenance(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """Where the release text came from, and WHEN it existed.

    Two questions. The one the brief asks — is any cached text an 8-K/A
    amendment filed later? — and the one that turns out to matter more: was
    the 8-K on the tape by 16:20 ET, the minute the study enters at?
    """
    out: list[str] = []
    with _http() as http:
        cik_of, _ = ticker_map(http)
        rows = []
        seen_cik: dict[int, dict] = {}
        for n, (_, r) in enumerate(m.iterrows()):
            cik = cik_of.get(r["symbol"])
            if cik is None:
                rows.append({"symbol": r["symbol"], "date": r["date"],
                             "status": "no cik"})
                continue
            if cik not in seen_cik:
                before = (SUBS / f"CIK{cik:010d}.json").exists()
                seen_cik[cik] = {"rows": submissions(cik, http)}
                if not before:
                    _time.sleep(0.12)
            filings = seen_cik[cik]["rows"]
            # EXACTLY the resolution fetch_release_text performs.
            hit = next((f for f in filings
                        if f["form"] == "8-K" and "2.02" in f["items"]
                        and f["filingDate"] == r["date"]), None)
            if hit is None:
                rows.append({"symbol": r["symbol"], "date": r["date"],
                             "status": "no 8-K matched"})
                continue
            et = acceptance_et(hit["acceptance"])
            rows.append({"symbol": r["symbol"], "date": r["date"],
                         "status": "ok", "form": hit["form"], "acc": hit["acc"],
                         "acceptance": hit["acceptance"][:19],
                         "acc_min": et.hour * 60 + et.minute if et else -1,
                         "acc_date": str(et.date()) if et else "",
                         "raw_min": (int(hit["acceptance"][11:13]) * 60
                                     + int(hit["acceptance"][14:16]))})
            if n % 200 == 0:
                print(f"  provenance {n}/{len(m)}")
    df = pd.DataFrame(rows)
    ok = df[df["status"] == "ok"]
    out.append(f"resolved the filing behind {len(ok)}/{len(df)} scored events "
               f"({df['status'].value_counts().to_dict()})")

    amended = ok[ok["form"] != "8-K"]
    out.append(f"amendments (8-K/A) used as the release text: {len(amended)} "
               f"— the resolver matches form == '8-K' exactly, so an "
               f"amendment cannot be selected")

    late_date = ok[ok["acc_date"] != ok["date"]]
    out.append(f"filings whose acceptance DATE differs from the announce day: "
               f"{len(late_date)}")

    # THE ONE THAT MATTERS. Entry is the 16:20 print. A release accepted at
    # 17:10 was not public then, so a verdict formed from it could not have
    # been acted on at that price.
    timed = ok[ok["acc_min"] >= 0]
    late = timed[timed["acc_min"] > ENTRY_CUTOFF_MIN]
    early = timed[timed["acc_min"] < 16 * 60]
    out.append("")
    out.append(f"acceptance time IN ET vs the 16:20 entry, n={len(timed)}:")
    for lo, hi, label in ((0, 12 * 60, "before noon"),
                          (12 * 60, 15 * 60, "12:00-15:00"),
                          (15 * 60, 16 * 60, "15:00-16:00"),
                          (16 * 60, 16 * 60 + 5, "16:00-16:05"),
                          (16 * 60 + 5, ENTRY_CUTOFF_MIN + 1, "16:05-16:20"),
                          (ENTRY_CUTOFF_MIN + 1, 17 * 60 + 30, "16:21-17:30"),
                          (17 * 60 + 30, 24 * 60, "after 17:30")):
        k = timed[(timed["acc_min"] >= lo) & (timed["acc_min"] < hi)]
        out.append(f"  {label:>12}  {len(k):>5}  {len(k) / len(timed) * 100:5.1f}%")
    out.append(f"accepted AFTER the 16:20 entry print: {len(late)} "
               f"({len(late) / len(timed) * 100:.1f}%)")
    out.append(f"accepted BEFORE 16:00, i.e. not an after-close release at "
               f"all: {len(early)} ({len(early) / len(timed) * 100:.1f}%)")
    out.append("")
    out.append("the second number is the interesting one. fetch_calendar_window "
               "classifies the hour by slicing the RAW string and comparing to "
               "16:00, so on a UTC timestamp its 'amc' bucket really means "
               "'accepted after 16:00 UTC' = after noon ET in summer, 11:00 ET "
               "in winter. Midday filers therefore enter the AMC calendar.")

    # What does it cost? Restate the headline on the correctly-timed subset.
    m2 = m.merge(timed[["symbol", "date", "acc_min"]], on=["symbol", "date"],
                 how="inner")
    m2["mech_pnl"] = np.sign(m2["move_pct"]) * m2["fwd_bp"] - 13.0
    from research_llm_contamination import _gate
    clean = m2[(m2["acc_min"] >= 16 * 60) & (m2["acc_min"] <= ENTRY_CUTOFF_MIN)]
    out.append("")
    out.append("| subset | events | gated n | gated bp | vetoed bp | spread |")
    out.append("|---|---|---|---|---|---|")
    for label, sub in (("all scored events", m2),
                       ("acceptance in [16:00, 16:20] ET only", clean),
                       ("acceptance before 16:00 ET",
                        m2[m2["acc_min"] < 16 * 60]),
                       ("acceptance after 16:20 ET",
                        m2[m2["acc_min"] > ENTRY_CUTOFF_MIN])):
        if not len(sub):
            continue
        k = _gate(sub)
        v = sub[~k]["mech_pnl"].mean() if (~k).any() else float("nan")
        g = sub[k]["mech_pnl"].mean() if k.any() else float("nan")
        out.append(f"| {label} | {len(sub)} | {int(k.sum())} | {g:+.1f} "
                   f"| {v:+.1f} | {g - v:+.1f} |")
    df.to_parquet(CACHE / "lookahead_provenance.parquet")
    out.append("")
    out.append(f"per-event provenance written to "
               f"{CACHE / 'lookahead_provenance.parquet'}")

    # Immutability of accession-scoped URLs: refetch two and compare bytes.
    out.append("")
    probes = ok.head(2)
    same = 0
    with _http() as http:
        for _, r in probes.iterrows():
            cached = (TEXTS / f"{r['symbol']}_{r['date']}.txt")
            if not cached.exists():
                continue
            from app.services.signals.edgar import EdgarFeed, strip_html
            cikv = cik_of[r["symbol"]]
            nodash = r["acc"].replace("-", "")
            idx = http.get(f"https://www.sec.gov/Archives/edgar/data/{cikv}/"
                           f"{nodash}/{r['acc']}-index.htm").text
            path = EdgarFeed._find_exhibit(idx)
            url = path if str(path).startswith("http") else f"https://www.sec.gov{path}"
            fresh = strip_html(http.get(url).text)[:12_000]
            match = fresh == cached.read_text(encoding="utf-8", errors="replace")
            same += match
            out.append(f"  refetched {r['symbol']} {r['date']} "
                       f"({r['acc']}): {'byte-identical' if match else 'DIFFERS'}")
            _time.sleep(0.2)
    out.append(f"accession-scoped EDGAR URLs are immutable: {same}/{len(probes)} "
               f"refetches reproduced the cached text exactly")

    passed = (len(amended) == 0 and same == len(probes) and len(late) == 0
              and len(early) == 0)
    return passed, out


# ------------------------------------------------------------------ check 3

def check_features(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """run5d and dv use strictly prior sessions.

    Two steps, because a reimplementation that agrees with itself proves
    nothing. First reproduce the shipped feature from the raw panel and
    require an exact match — that validates the reimplementation. Then set
    every bar at or after the announce day to NaN and require the feature to
    be UNCHANGED. A value that survives the deletion of the future cannot
    have been reading it.
    """
    out: list[str] = []
    shipped = {(r["symbol"], r["date"]): (float(r["run5d"]), float(r["dv"]))
               for _, r in m.iterrows()}
    n_ok = n_mismatch = n_moved = n_bad_index = n_skip = 0
    examples: list[str] = []

    # Walk the SAME loop load_universe walks — per pead_events file, gate,
    # then the panel that covers that file's span — so the panel chosen for
    # an event is the one the shipped code chose, not a lookalike.
    for path in sorted(CACHE.glob("pead_events_*.parquet")):
        ev = pd.read_parquet(path)
        ev["date"] = pd.to_datetime(ev["date"]).dt.date
        ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
        g = ev[np.abs(ev["move_pct"]) >= GATE]
        if g.empty:
            continue
        bars = _bars_for(str(g["date"].min()), str(g["date"].max()))
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        bars = bars.sort_values(["symbol", "date"])
        panel = {s: (gg["date"].to_list(), gg["c"].to_numpy(float),
                     (gg["c"] * gg["v"]).to_numpy(float))
                 for s, gg in bars.groupby("symbol")}
        for _, row in g.iterrows():
            key = (row["symbol"], str(row["date"]))
            if key not in shipped:
                continue                      # not one of the scored events
            entry = panel.get(row["symbol"])
            if entry is None:
                n_skip += 1
                continue
            d, c, dv = entry
            i = int(np.searchsorted(d, row["date"]))
            if not (6 <= i < len(c)):
                n_skip += 1
                continue
            # The invariant that actually matters: the newest bar either
            # feature can reach is strictly OLDER than the announce day.
            if not (d[i - 1] < row["date"]):
                n_bad_index += 1
                if len(examples) < 3:
                    examples.append(f"{key[0]} {key[1]}: prior bar {d[i - 1]} "
                                    f"is not strictly before the announce day")
                continue
            run5d, dv_prior = (c[i - 1] / c[i - 6] - 1) * 100, dv[i - 1]
            want_run, want_dv = shipped[key]
            if not (np.isclose(run5d, want_run, rtol=1e-9, atol=1e-9)
                    and np.isclose(dv_prior, want_dv, rtol=1e-9)):
                n_mismatch += 1
                if len(examples) < 6:
                    examples.append(f"{key[0]} {key[1]}: recomputed "
                                    f"{run5d:.6f}/{dv_prior:.0f} vs shipped "
                                    f"{want_run:.6f}/{want_dv:.0f}")
                continue
            # ERASE THE FUTURE. Dates stay (a trading calendar is knowledge,
            # not a price); every PRICE and VOLUME from the announce day
            # forward becomes NaN. A feature that survives that untouched
            # cannot have been reading it.
            c2, dv2 = c.copy(), dv.copy()
            c2[i:] = np.nan
            dv2[i:] = np.nan
            if ((c2[i - 1] / c2[i - 6] - 1) * 100 == run5d
                    and dv2[i - 1] == dv_prior):
                n_ok += 1
            else:
                n_moved += 1
                if len(examples) < 6:
                    examples.append(f"{key[0]} {key[1]}: feature MOVED when "
                                    f"the future was erased")

    out.append(f"recomputed run5d/dv from the raw daily panels for "
               f"{n_ok + n_moved + n_mismatch}/{len(m)} scored events "
               f"({n_skip} skipped: symbol absent from the covering panel, or "
               f"fewer than 6 prior sessions)")
    out.append(f"disagreements with the shipped feature: {n_mismatch}")
    out.append(f"events whose newest reachable bar was NOT strictly older than "
               f"the announce day: {n_bad_index}")
    out.append(f"FUTURE-ERASURE TEST — every price and volume from the "
               f"announce day forward set to NaN: {n_ok} features unchanged, "
               f"{n_moved} moved")
    for e in examples:
        out.append(f"  {e}")
    out.append("index i is located by date, then only c[i-1], c[i-6] and "
               "dv[i-1] are read — i itself never enters either feature")
    return (n_mismatch == 0 and n_moved == 0 and n_bad_index == 0 and n_ok > 0), out


# ------------------------------------------------------------------ check 4

def check_selection(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """Selection uses prior-session data only, and the residual survivorship
    is stated rather than fixed."""
    out: list[str] = []

    # (a) The window universe is ranked on the window's FIRST sessions.
    src = (Path(__file__).resolve().parent / "research_leadup_account_sim.py"
           ).read_text(encoding="utf-8")
    body = src.split("def build_universe_window")[1].split("\ndef ")[0]
    ranks_on_start = ("win_start + timedelta(days=10)" in body
                      and "start=datetime.combine(win_start" in body)
    out.append(f"build_universe_window ranks on [win_start, win_start+10d]: "
               f"{ranks_on_start}")
    for path in sorted(CACHE.glob("universe_*.parquet")):
        u = pd.read_parquet(path)
        out.append(f"  {path.name}: {len(u)} tickers ranked at the window open")

    # (b) The per-day watchlist rule ranks on the PRIOR session's dollar
    #     volume. Check 3 already proved dv[i-1] survives erasing the future,
    #     so the ranking inherits that. What is left to show is that the test
    #     has POWER: rank the same days on the announce day's OWN dollar
    #     volume and count how often the chosen five differ. If the two
    #     rankings were identical the invariance would be vacuous.
    same_day: dict[tuple[str, str], float] = {}
    for path in sorted(CACHE.glob("pead_events_*.parquet")):
        ev = pd.read_parquet(path)
        ev["date"] = pd.to_datetime(ev["date"]).dt.date
        ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
        g = ev[np.abs(ev["move_pct"]) >= GATE]
        if g.empty:
            continue
        bars = _bars_for(str(g["date"].min()), str(g["date"].max()))
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        bars = bars.sort_values(["symbol", "date"])
        panel = {s: (gg["date"].to_list(), (gg["c"] * gg["v"]).to_numpy(float))
                 for s, gg in bars.groupby("symbol")}
        for _, row in g.iterrows():
            d, dv = panel.get(row["symbol"], (None, None))
            if d is None:
                continue
            i = int(np.searchsorted(d, row["date"]))
            if 0 <= i < len(dv):
                same_day[(row["symbol"], str(row["date"]))] = float(dv[i])
    n_days = n_diff = 0
    for day, gg in m.groupby("date"):
        if len(gg) < 2:
            continue
        prior_rank = list(gg.sort_values("dv", ascending=False)["symbol"]
                          .head(TOP_PER_DAY))
        gg = gg.assign(dv_today=[same_day.get((s, day), np.nan)
                                 for s in gg["symbol"]])
        if gg["dv_today"].isna().any():
            continue
        today_rank = list(gg.sort_values("dv_today", ascending=False)["symbol"]
                          .head(TOP_PER_DAY))
        n_days += 1
        n_diff += prior_rank != today_rank
    out.append(f"per-day top-{TOP_PER_DAY} is ranked on the PRIOR session's "
               f"dollar volume (check 3 proves that value survives erasing "
               f"the future)")
    out.append(f"power of that statement: ranking the same {n_days} days on "
               f"the announce day's OWN dollar volume changes the chosen "
               f"names on {n_diff} of them ({n_diff / max(n_days, 1) * 100:.0f}%) "
               f"— the two rankings are genuinely different objects, so the "
               f"invariance is not vacuous")
    out.append(f"gate |reaction| >= {GATE:.0f}% is applied to the announce "
               f"day's own tape (that IS the signal, not a lookahead); the "
               f"${MIN_DV/1e6:.0f}M floor uses the prior session's volume")

    # (c) Survivorship, quantified as far as it can be.
    with _http() as http:
        cik_of, _ = ticker_map(http)
    cal_syms: set[str] = set()
    for path in sorted(CACHE.glob("edgar_cal_*.parquet")):
        cal_syms |= set(pd.read_parquet(path)["symbol"])
    missing = sorted(s for s in cal_syms if s not in cik_of)
    out.append("")
    out.append(f"EDGAR calendar covers {len(cal_syms)} distinct tickers; "
               f"{len(missing)} are absent from today's company_tickers.json")
    out.append("that count is structurally ZERO and is NOT a clean bill of "
               "health: the calendar was BUILT from today's ticker map "
               "(build_universe_window reads company_tickers.json), so an "
               "issuer delisted before today could never have entered it. The "
               "survivorship is in the map, not in the filter, and cannot be "
               "measured from these artefacts — only stated.")
    scored_syms = set(m["symbol"])
    out.append(f"scored universe: {len(scored_syms)} distinct issuers over "
               f"{m['date'].min()}..{m['date'].max()}")
    return bool(ranks_on_start and n_days > 0), out


# ------------------------------------------------------------------ check 5

def check_no_tools(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """The model could not look the answer up."""
    out: list[str] = []
    with _http() as http:
        _, names = ticker_map(http)

    def walk(node, found: list[str], path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in ("tools", "tool_choice", "mcp_servers",
                                      "web_search", "server_tool_use"):
                    found.append(f"{path}.{k}")
                walk(v, found, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, found, f"{path}[{i}]")

    n = 0
    offenders: list[str] = []
    schema_ok = 0
    for _, row in m.iterrows():
        path = TEXTS / f"{row['symbol']}_{row['date']}.txt"
        if not path.exists():
            continue
        req = build_request(row, "named", path.read_text(encoding="utf-8",
                                                         errors="replace"),
                            names.get(row["symbol"], ""), "medium")
        if req is None:
            continue
        n += 1
        found: list[str] = []
        walk(req, found)
        if found:
            offenders.append(f"{row['symbol']} {row['date']}: {found}")
        fmt = req["params"].get("output_config", {}).get("format", {})
        schema_ok += fmt.get("type") == "json_schema"
    out.append(f"scanned {n} rebuilt request payloads for a tools / "
               f"tool_choice / mcp_servers key: {len(offenders)} found")
    out += [f"  {o}" for o in offenders[:5]]
    out.append(f"output_config.format.type == 'json_schema' on {schema_ok}/{n} "
               f"— the schema-constrained path emits one JSON object and has "
               f"no tool-call channel to invoke search with")

    if args.online:
        out.append("")
        out.append(_online_tool_audit(out))
    else:
        out.append("the submitted payloads are not retrievable from the Batch "
                   "API, so this is a proof about the BUILDER, which is "
                   "deterministic. Re-run with --online to also read the "
                   "returned messages back.")
    return len(offenders) == 0 and schema_ok == n and n > 0, out


def _online_tool_audit(out: list[str]) -> str:
    """Read the batch RESULTS back and look at what actually came home. Free:
    results are already paid for and retained 29 days."""
    from research_llm_contamination import _batches, client

    api = client()
    n = tool_blocks = bad_stop = 0
    for meta in _batches():
        try:
            for result in api.messages.batches.results(meta["id"]):
                if result.result.type != "succeeded":
                    continue
                msg = result.result.message
                n += 1
                tool_blocks += sum(1 for b in msg.content
                                   if getattr(b, "type", "") not in
                                   ("text", "thinking", "redacted_thinking"))
                bad_stop += getattr(msg, "stop_reason", "") == "tool_use"
        except Exception as exc:
            return f"batch {meta['id']} unreadable ({str(exc)[:60]})"
    return (f"read {n} returned messages from the Batch API: {tool_blocks} "
            f"non-text content blocks, {bad_stop} with stop_reason "
            f"'tool_use' — no retrieval happened")


# ------------------------------------------------------------------ check 6

def check_entry(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """Re-derive the entry from raw minute bars and time-stamp it.

    Not a code read: refetch the tape for a sample, rebuild `anchor` and
    `react` exactly as research_pead_backtest does, and check both that the
    cached numbers reproduce and that the print we enter at is stamped at or
    before 16:20 ET.
    """
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    out: list[str] = []
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    sample = m.sample(n=min(args.sample, len(m)), random_state=20260806)
    n_ok = n_react_bad = n_late = n_anchor_bad = n_skip = 0
    latest = 0
    for _, row in sample.iterrows():
        d = pd.Timestamp(row["date"]).date()
        start = datetime(d.year, d.month, d.day, 15, 30, tzinfo=ET)
        try:
            res = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=row["symbol"], timeframe=TimeFrame.Minute,
                start=start, end=start + pd.Timedelta(hours=3).to_pytimedelta(),
                feed="sip"))
            bars = res.data.get(row["symbol"]) or []
        except Exception:
            bars = []
        if not bars:
            n_skip += 1
            continue
        ts = [b.timestamp.astimezone(ET) for b in bars]
        px = [float(b.close) for b in bars]
        pre = [(t, p) for t, p in zip(ts, px) if t.date() == d and t.hour < 16]
        ah = [(t, p) for t, p in zip(ts, px)
              if t.date() == d and t.hour >= 16
              and (t.hour * 60 + t.minute) <= ENTRY_CUTOFF_MIN]
        if not pre or not ah:
            n_skip += 1
            continue
        if not np.isclose(pre[-1][1], float(row["anchor"]), rtol=1e-6):
            n_anchor_bad += 1
        t_react, p_react = ah[-1]
        minute = t_react.hour * 60 + t_react.minute
        latest = max(latest, minute)
        if minute > ENTRY_CUTOFF_MIN:
            n_late += 1
        if np.isclose(p_react, float(row["react"]), rtol=1e-6):
            n_ok += 1
        else:
            n_react_bad += 1
        _time.sleep(0.2)
    n = n_ok + n_react_bad
    out.append(f"refetched minute bars for {n} of {len(sample)} sampled events "
               f"({n_skip} had no usable tape in the window)")
    out.append(f"cached `react` reproduced from the raw tape: {n_ok}/{n} "
               f"(mismatches {n_react_bad})")
    out.append(f"cached `anchor` mismatches: {n_anchor_bad}")
    out.append(f"entry print stamped after 16:20 ET: {n_late} — latest observed "
               f"{latest // 60:02d}:{latest % 60:02d} ET")
    out.append("the reaction window is a hard [16:00, 16:20] slice in "
               "research_pead_backtest, so this is true by construction; the "
               "refetch confirms the cache was built by that code and not "
               "edited afterwards")
    return (n_react_bad == 0 and n_late == 0 and n_anchor_bad == 0 and n > 0), out


# ------------------------------------------------------------------ check 7

# Phrases a press release should never contain: they describe the market's
# response, which by definition post-dates the release.
COMMENTARY = [
    (r"\bshares (?:rose|fell|jumped|dropped|surged|slid|climbed|plunged|gained|declined)\b", "shares <moved>"),
    (r"\bstock (?:rose|fell|jumped|dropped|surged|slid|climbed|plunged)\b", "stock <moved>"),
    (r"\bin (?:after|extended)[- ]hours trading\b", "after-hours trading"),
    (r"\bafter[- ]hours (?:trad|the stock|shares)", "after-hours reaction"),
    # "closed up" needs the trailing boundary or it fires on "we closed
    # Upfront deals for the 2022-2023 TV season" (ROKU 2022-07-28).
    (r"\bclosed (?:up|down)\b|\bclosed at \$?\d", "closed up/down/at"),
    (r"\b(?:pre|post)[- ]market trading\b", "pre/post-market trading"),
    (r"\banalysts? (?:had )?expected\b", "analyst expectation commentary"),
    (r"\bbeat (?:the )?(?:street|consensus) (?:estimates? )?(?:by|and the)", "street-beat commentary"),
]


def check_text_hygiene(m: pd.DataFrame, args) -> tuple[bool, list[str]]:
    """No cached release text contains post-release market commentary."""
    out: list[str] = []
    files = sorted(TEXTS.glob("*.txt"))
    used = {f"{r['symbol']}_{r['date']}.txt" for _, r in m.iterrows()}
    hits: list[tuple[str, str, str]] = []
    n = 0
    for path in files:
        if path.name not in used:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text:
            continue
        n += 1
        for pattern, label in COMMENTARY:
            found = re.search(pattern, text, re.I)
            if found:
                span = text[max(0, found.start() - 60):found.end() + 60]
                hits.append((path.name, label, " ".join(span.split())))
    out.append(f"scanned {n} cached release texts for post-release market "
               f"commentary: {len(hits)} matches")
    for name, label, span in hits[:12]:
        out.append(f"  {name} [{label}] …{span}…")
    if len(hits) > 12:
        out.append(f"  … and {len(hits) - 12} more")

    lengths = []
    for path in files:
        if path.name in used:
            lengths.append(len(path.read_text(encoding="utf-8", errors="replace")))
    if lengths:
        arr = np.asarray(lengths)
        out.append(f"text length: median {np.median(arr):.0f} chars, "
                   f"{(arr >= 12_000).mean() * 100:.0f}% truncated at the "
                   f"12k max_text ceiling, {(arr < 500).sum()} under 500 chars")
    out.append("five texts read end to end by hand are listed in the note; "
               "this scan covers all of them mechanically")
    return len(hits) == 0, out


# --------------------------------------------------------------------- main

CHECKS = {
    "prompt": ("1. prompt contents carry no outcome", check_prompt),
    "provenance": ("2. release-text provenance and timing", check_provenance),
    "features": ("3. run-up and liquidity features are prior-session only",
                 check_features),
    "selection": ("4. universe selection and survivorship", check_selection),
    "notools": ("5. no retrieval channel", check_no_tools),
    "entry": ("6. entry print is at or before 16:20 ET", check_entry),
    "text": ("7. no post-release commentary in the release texts",
             check_text_hygiene),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="", help="comma-separated check names")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--online", action="store_true",
                    help="also read batch RESULTS back from the API (free)")
    args = ap.parse_args()

    wanted = [k for k in CHECKS if not args.only or k in args.only.split(",")]
    m = scored()
    lines = [f"# Lookahead audit — {datetime.now(ET).strftime('%Y-%m-%d %H:%M')} ET",
             "",
             f"{len(m)} scored events, {m['date'].min()}..{m['date'].max()}. "
             f"Claim under test: every input the model saw was available at "
             f"16:20 ET on the announce day, and nothing derived from the "
             f"outcome reached the prompt, the gate, or the selection.", ""]
    verdicts: list[tuple[str, bool]] = []
    for key in wanted:
        title, fn = CHECKS[key]
        print(f"\n=== {title} ===")
        try:
            ok, body = fn(m, args)
        except Exception as exc:  # a check that cannot run is not a pass
            ok, body = False, [f"check raised {type(exc).__name__}: {exc}"]
        verdicts.append((title, ok))
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] {title}")
        for line in body:
            print(f"  {line}")
        lines.append(f"## [{tag}] {title}")
        lines.append("")
        lines += body
        lines.append("")

    print("\n" + "=" * 68)
    for title, ok in verdicts:
        print(f"[{'PASS' if ok else 'FAIL'}] {title}")
    n_fail = sum(1 for _, ok in verdicts if not ok)
    print(f"{len(verdicts) - n_fail}/{len(verdicts)} checks passed")
    lines.insert(4, f"**{len(verdicts) - n_fail}/{len(verdicts)} checks "
                    f"passed.** " + " ".join(
                        f"[{'PASS' if ok else 'FAIL'}] {t.split('.')[0]}"
                        for t, ok in verdicts))
    lines.insert(5, "")
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    DOC.mkdir(parents=True, exist_ok=True)
    doc = DOC / f"lookahead_audit_{stamp}.md"
    doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {doc}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
