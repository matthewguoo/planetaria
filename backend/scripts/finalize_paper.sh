#!/usr/bin/env bash
# Everything between "the verdicts landed" and "the paper is on disk".
#
# Ordered by dependency, and it stops on the first failure rather than
# publishing a paper built on half a pipeline. Run from backend/.
#
#   bash scripts/finalize_paper.sh
set -euo pipefail

PY=./.venv/Scripts/python.exe
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
OUTDIR="${1:-C:/Users/matth/AppData/Local/Temp/claude/C--Users-matth-Desktop-planetaria/06685ccc-f5fc-4d16-83ca-c859bd8273cf/scratchpad}"

echo "=== 1/7 collect verdicts (banks real cost, settles the budget guard) ==="
$PY scripts/research_llm_contamination.py collect | tail -6

echo; echo "=== 2/7 per-event paths for any newly-qualifying events ==="
$PY scripts/research_event_panel.py paths --windows ten 2>&1 | tail -3

echo; echo "=== 3/7 score the arms ==="
$PY scripts/research_llm_contamination.py arms --effort medium --panel v2 --windows ten 2>&1 | tail -22

echo; echo "=== 4/7 best config, mutations, trade export (v2 panel) ==="
$PY scripts/research_holding_period.py best --panel v2 2>&1 | tail -9
$PY scripts/research_holding_period.py mutations --panel v2 2>&1 | tail -4
$PY scripts/research_holding_period.py trades --panel v2 2>&1 | tail -2

echo; echo "=== 5/7 assemble every number the paper cites ==="
$PY scripts/build_report_data.py 2>&1 | tail -3

echo; echo "=== 6/7 render the paper (screen + print builds) ==="
$PY scripts/build_paper.py | tail -1
$PY scripts/build_paper.py --print | tail -1

echo; echo "=== 7/7 PDF ==="
"$CHROME" --headless --disable-gpu --no-sandbox \
  --run-all-compositor-stages-before-draw --virtual-time-budget=60000 \
  --print-to-pdf-no-header \
  --print-to-pdf="$OUTDIR/planetaria-llm-pead-working-paper.pdf" \
  "file:///C:/Users/matth/Desktop/planetaria/docs/report_print.html" 2>&1 | tail -1

$PY - <<'PYEOF'
from pathlib import Path
import re, os
from pypdf import PdfReader
p = Path(os.environ.get("OUTDIR", "")) if os.environ.get("OUTDIR") else None
p = (p or Path(r"C:/Users/matth/AppData/Local/Temp/claude/C--Users-matth-Desktop-planetaria/06685ccc-f5fc-4d16-83ca-c859bd8273cf/scratchpad")) / "planetaria-llm-pead-working-paper.pdf"
r = PdfReader(str(p))
txt = "\n".join(x.extract_text() for x in r.pages)
rows = len(re.findall(r"\btaken\b|\bdeclined\b", txt, re.I))
print(f"PDF: {len(r.pages)} pages, {p.stat().st_size/1e6:.1f} MB, {rows} appendix row markers")
if rows < 100:
    raise SystemExit("REFUSED: the appendix did not print — check the print build")
PYEOF
echo; echo "done."
