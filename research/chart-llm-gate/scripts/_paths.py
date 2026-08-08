"""Filesystem anchors for the chart-reading study.

Same contract as `research/pead-llm-gate/scripts/_paths.py`: the study READS
the app (`BACKEND` on sys.path, so `from app.config import get_settings`
resolves for the Alpaca keys) and the app never reads the study. Callers put
BACKEND on sys.path themselves rather than having this module do it as an
import side effect, so the dependency stays visible at the call site.
"""

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent          # …/research/chart-llm-gate/scripts
STUDY = SCRIPTS.parent                             # …/research/chart-llm-gate
ROOT = STUDY.parents[1]                            # repo root

BACKEND = ROOT / "backend"

CACHE = STUDY / "cache"                            # bars, setups, verdicts
NOTES = STUDY / "notes"                            # dated run logs
BARS = CACHE / "bars"                              # 1m SIP bars, one file per symbol
VERDICTS = CACHE / "verdicts"                      # results_{arm}_{model}.jsonl
