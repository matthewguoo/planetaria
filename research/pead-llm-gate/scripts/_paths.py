"""Filesystem anchors for the study.

Before 2026-08-06 every module derived its own paths from `Path(__file__)`,
which worked only because they all sat at the same depth under
`backend/scripts/`. Moving the study out of `backend/` broke that assumption
in fourteen files at once, so the anchors live here now and the depth is
computed once.

The rule the anchors encode: the study READS the app (`BACKEND` on sys.path,
so `from app.config import get_settings` resolves) and the app never reads
the study. `DOCS` is the published paper — the study writes `report_data.json`
and `report.html` there and nothing else.
"""

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent          # …/research/pead-llm-gate/scripts
STUDY = SCRIPTS.parent                             # …/research/pead-llm-gate
ROOT = STUDY.parents[1]                            # repo root

# The app the study imports from. Callers put this on sys.path themselves —
# doing it here as an import side effect would make the dependency invisible
# at the call site.
BACKEND = ROOT / "backend"

CACHE = STUDY / "cache"                            # bars, filings, verdicts
NOTES = STUDY / "notes"                            # dated run logs
DOCS = ROOT / "docs"                               # the paper (published output)
PUBLIC = ROOT / "frontend" / "public"              # study payloads the Lab reads
ENV = ROOT / ".env"
