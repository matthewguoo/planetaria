"""Filesystem anchors for the study.

Before 2026-08-06 every module derived its own paths from `Path(__file__)`,
which worked only because they all sat at the same depth under
`backend/scripts/`. Moving the study out of `backend/` broke that assumption
in fourteen files at once, so the anchors live here now and the depth is
computed once.

The rule the anchors encode: the study READS the app (`BACKEND` on sys.path,
so `from app.config import get_settings` resolves) and the app never reads
the study.

`PAPER` is the study's own paper — template, payload and both rendered
formats. It sat in `docs/` until 2026-08-07 on the theory that a published
artefact belongs with the docs, which does not survive contact with two
facts: `report_template.html` is 205 KB of working source rather than
anything published, and `research/` is organised one directory per study, so
a second study's `report.html` would have collided. A study that cannot be
moved or archived as a unit is not really self-contained. `DOCS` now holds
only what is genuinely cross-cutting — the explainer, the runbook, the
briefs.
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
DATA = STUDY / "data"                              # committed sweep exports (CSV)
PAPER = STUDY / "paper"                            # template, payload, renders
DOCS = ROOT / "docs"                               # cross-cutting docs only
ENV = ROOT / ".env"

# Stage exports: the trade table the paper's appendix is built from, plus the
# curve and progress files. These lived in `frontend/public/` while a UI read
# them live; that UI is gone, and a study writing into the frontend's static
# assets was always the wrong direction of dependency.
PAYLOAD = STUDY / "payload"
