#!/usr/bin/env bash
# Everything between "the verdicts landed" and "the paper is on disk".
#
# Ordered by dependency, and it stops on the first failure rather than
# publishing a paper built on half a pipeline. Anchors itself to the study,
# so it runs from anywhere:
#
#   bash scripts/finalize_paper.sh [outdir-for-the-pdf]
#
# The PDF lands in paper/ next to the HTML builds unless an outdir is given.
# (The old header said "run from backend/" and defaulted the PDF into a
# long-dead Claude session scratchpad — both stale since the study moved out
# of backend/scripts on 2026-08-06.)
set -euo pipefail

STUDY="$(cd "$(dirname "$0")/.." && pwd)"
cd "$STUDY"
# Chrome and pypdf want Windows-style paths; Git Bash pwd gives /c/... form.
STUDY_WIN="$(cygpath -m "$STUDY")"
PY=../../backend/.venv/Scripts/python.exe
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
OUTDIR="${1:-$STUDY_WIN/paper}"
export OUTDIR

echo "=== 1/9 collect verdicts (banks real cost, settles the budget guard) ==="
$PY scripts/research_llm_contamination.py collect | tail -6

echo; echo "=== 2/9 per-event paths for any newly-qualifying events ==="
$PY scripts/research_event_panel.py paths --windows ten 2>&1 | tail -3

echo; echo "=== 3/9 score the arms ==="
$PY scripts/research_llm_contamination.py arms --effort medium --panel v2 --windows ten 2>&1 | tail -22

echo; echo "=== 4/9 best config, mutations, trade export (v2 panel) ==="
$PY scripts/research_holding_period.py best --panel v2 2>&1 | tail -9
$PY scripts/research_holding_period.py mutations --panel v2 2>&1 | tail -4
$PY scripts/research_holding_period.py trades --panel v2 2>&1 | tail -2

# Quote measurement is incremental and cached — this is a no-op once the panel
# is covered, and picks up only newly-qualifying events. It is the slowest
# stage in the chain by a wide margin (~4 SIP requests per event), which is
# why it caches rather than re-measuring.
echo; echo "=== 5/9 measured fill costs (cached; only new events are fetched) ==="
$PY scripts/research_spread_model.py measure 2>&1 | tail -8

echo; echo "=== 6/9 the out-of-training test across model cutoffs ==="
$PY scripts/research_out_of_training.py report 2>&1 | tail -12

echo; echo "=== 7/9 assemble every number the paper cites ==="
$PY scripts/build_report_data.py 2>&1 | tail -3

echo; echo "=== 8/9 render the paper (screen + print builds) ==="
$PY scripts/build_paper.py | tail -1
$PY scripts/build_paper.py --print | tail -1

echo; echo "=== 9/9 PDF ==="
"$CHROME" --headless --disable-gpu --no-sandbox \
  --run-all-compositor-stages-before-draw --virtual-time-budget=60000 \
  --print-to-pdf-no-header \
  --print-to-pdf="$OUTDIR/planetaria-llm-pead-working-paper.pdf" \
  "file:///$STUDY_WIN/paper/report_print.html" 2>&1 | tail -1

$PY - <<'PYEOF'
from pathlib import Path
import re, os
from pypdf import PdfReader
p = Path(os.environ["OUTDIR"]) / "planetaria-llm-pead-working-paper.pdf"
r = PdfReader(str(p))
txt = "\n".join(x.extract_text() for x in r.pages)
rows = len(re.findall(r"\btaken\b|\bdeclined\b", txt, re.I))
print(f"PDF: {len(r.pages)} pages, {p.stat().st_size/1e6:.1f} MB, {rows} appendix row markers")
if rows < 100:
    raise SystemExit("REFUSED: the appendix did not print — check the print build")
PYEOF
echo; echo "done."
