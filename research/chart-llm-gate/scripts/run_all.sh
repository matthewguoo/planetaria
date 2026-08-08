#!/usr/bin/env bash
# Every LLM arm, in the order the study wants them.
#
# ONE MODEL AT A TIME, because llama-server holds one set of weights and both
# of these fill a 24GB card on their own. Start the server for `phi4` in its
# own shell, run this with `phi4`, then swap the server to `qwen3` and run it
# again with `qwen3`.
#
#   scripts/research_chart_gate.py serve --model phi4     # shell 1
#   scripts/run_all.sh phi4                               # shell 2
#
# Volumes differ by arm on purpose. `gate` and `outcome` carry the result and
# get the full 6,000-setup panel. The controls only have to be well enough
# powered to detect a spread SURVIVING where it should collapse, which needs
# far fewer. qwen3 is a capability control, not a second study, so it takes a
# subset of everything.
set -euo pipefail
MODEL="${1:?usage: run_all.sh <phi4|qwen3>}"
P=../../backend/.venv/Scripts/python.exe
cd "$(dirname "$0")/.."

if [ "$MODEL" = "qwen3" ]; then
  GATE=2000; OUTCOME=2000; CTX=2000; SHUF=1000; COLD=750; SYNTH=400; OPT=400
else
  GATE=0;    OUTCOME=0;    CTX=0;    SHUF=2000; COLD=0;   SYNTH=750; OPT=1000
fi
lim() { [ "$1" -eq 0 ] && echo "" || echo "--limit $1"; }

# outcome first, then its context twin: those two are the conclusion, and the
# ablation between them only means anything if BOTH exist, so they run back to
# back before any control gets compute. Everything after is a control.
for spec in "outcome:$OUTCOME" "outcome_ctx:$CTX" "gate:$GATE" \
            "shuffled:$SHUF" "coldread:$COLD" "synthetic:$SYNTH" \
            "gateopt:$OPT"; do
  arm="${spec%%:*}"; n="${spec##*:}"
  echo "=== $MODEL / $arm ==="
  # shellcheck disable=SC2046
  $P scripts/research_chart_gate.py run --model "$MODEL" --arm "$arm" $(lim "$n")
done
echo "=== $MODEL: all arms done ==="
