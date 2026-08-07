"""Render the working paper from the harness output.

docs/report_template.html holds the prose, the design system and the render
code; every NUMBER comes from docs/report_data.json and the trade appendix
from frontend/public/study-trades.json. Nothing is transcribed by hand, so
the paper cannot drift from the study that produced it — re-run the pipeline
and the paper restates itself.

The appendix is 1,312 rows and the raw export is 1.0 MB, most of it repeated
JSON keys. It is re-encoded here as a column header plus one array per row,
which is the difference between a page that streams and a page that hangs.

Run (after build_report_data.py):
    .venv/Scripts/python.exe scripts/build_paper.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "report_data.json"
TRADES = ROOT / "frontend" / "public" / "study-trades.json"
TEMPLATE = ROOT / "docs" / "report_template.html"
OUT = ROOT / "docs" / "report.html"

# Everything the page renders. Anything not listed is dropped, which is how
# a 228 KB data file and a 1.0 MB trade export become a payload that fits in
# a page without a loading spinner.
KEEP = ("model", "effort", "universe_n", "scored_n", "paths_n", "gated_n",
        "vetoed_n", "span", "train_n", "test_n", "gate_pct", "top_per_day",
        "min_dv_musd", "costs_bp", "timing_corrected", "spec",
        "audit", "horizons", "headline", "years", "liquidity",
        "gradient", "brackets", "shipped_bracket", "mutations", "spy",
        "effort_cal", "spend_usd",
        # Added 2026-08-06. This is an ALLOWLIST: a key absent here is silently
        # dropped from the payload and the page renders its guard instead of
        # its table, which looks exactly like "the data has not been computed
        # yet". Anything new in build_report_data must be listed.
        "funnel_v2", "reaction_shape", "model_compare",
        "holdout", "arms", "contamination", "regimes",
        # Added 2026-08-06 with the fill-cost measurement and the
        # out-of-training test. Listed here at the same time as the keys were
        # added to build_report_data — that ordering is the whole discipline,
        # because the failure mode is silent.
        "spreads", "xmodel", "late_filers", "liquidity_study",
        # Added 2026-08-06 with Appendix B. The warning above is accurate:
        # omitting this rendered "the earlier-cutoff addendum is not in this
        # build: not computed" over a fully computed result.
        "legacy",
        # The early/late split behind Section 5.5's callout.
        "early_late",
        # Section 5.5's position-limit sweep.
        "capacity")

TRADE_COLS = ["date", "sym", "dir", "conf", "guid", "move", "run5d", "dvM",
              "gated", "why", "t1", "t3", "shipped", "exit", "summary"]


def trades() -> dict:
    raw = json.loads(TRADES.read_text(encoding="utf-8"))
    rows = []
    for t in raw["trades"]:
        rows.append([
            t["date"], t["sym"], t["dir"], t["conf"], t["guid"],
            t["move"], t["run5d"], t["dvM"],
            1 if t["gated"] else 0, t["why"],
            t["ret"]["t1"], t["ret"]["t3"], t["ret"]["shipped"],
            t["exit"]["shipped"],
            (t["summary"] or "")[:240],
        ])
    return {"cols": TRADE_COLS, "rows": rows, "n": len(rows),
            "gated": raw["gated"], "policies": raw["policies"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print", dest="print_mode", action="store_true",
                    help="write report_print.html: light palette, every "
                         "appendix row, no pagination — the PDF source")
    args = ap.parse_args()

    data = json.loads(DATA.read_text(encoding="utf-8"))
    payload = {k: data[k] for k in KEEP if k in data}
    # Baked into the payload rather than read from location.search at runtime:
    # a `file://` URL's query string does not survive every viewer (the
    # in-app preview strips it, and the first PDF attempt printed zero
    # appendix rows because of exactly that). A flag in the document cannot
    # be lost in transit.
    payload["print_mode"] = bool(args.print_mode)
    curve = data.get("curve_raw") or {}
    payload["curve"] = {k: curve.get(k) for k in
                        ("dates", "series", "labels", "stats", "span", "note")}
    payload["trades"] = trades()

    blob = json.dumps(payload, separators=(",", ":"))
    html = TEMPLATE.read_text(encoding="utf-8")
    if "/*DATA*/" not in html:
        raise SystemExit("template has no /*DATA*/ slot")
    out = OUT.with_name("report_print.html") if args.print_mode else OUT
    # THE FILE MUST DECLARE ITS OWN ENCODING. The template opens at <title>
    # with no <head>, because the artifact publisher supplies one. Served as
    # a plain file over HTTP that head never arrives, Python's http.server
    # sends `text/html` with no charset parameter, and the browser falls back
    # to the locale default — windows-1252 here. Every em dash became "â€”",
    # 365 times, and Chrome printed the PDF from that same misread document.
    # A meta in the first bytes is what makes the file correct standalone; it
    # is harmless inside the publisher's wrapper, which already says utf-8.
    doc = '<meta charset="utf-8">\n' + html.replace("/*DATA*/", blob)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB) — "
          f"payload {len(blob) / 1024:.0f} KB, "
          f"{payload['trades']['n']} appendix rows"
          + (" [print build]" if args.print_mode else ""))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
