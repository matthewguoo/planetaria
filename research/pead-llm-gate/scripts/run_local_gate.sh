#!/bin/bash
# Unattended driver for the local-weights arms. Two models, three arms each,
# one 3090 — so the models are strictly sequential (the second cannot load
# until the first releases its VRAM) while the arms within a model share one
# warm server.
#
# Arm order is deliberate: notext first because it is the cheapest and it is
# the one that decides whether the other two mean anything. If a model turns
# out to have real recall of these events, that is worth knowing before
# spending six hours on its named arm, not after.
#
# Every stage is resumable. research_local_gate.py skips any custom_id already
# on disk, so re-running this script after a crash picks up where it stopped
# and re-buys nothing.
set -u

PY="C:/Users/matth/Desktop/planetaria/backend/.venv/Scripts/python.exe"
SCRIPTS="C:/Users/matth/Desktop/planetaria/research/pead-llm-gate/scripts"
LLAMA="C:/Users/matth/llamacpp/llama-server.exe"
GGUF="C:/Users/matth/models"
LOG="C:/Users/matth/Desktop/planetaria/research/pead-llm-gate/cache/llm_contam"

cd "$SCRIPTS" || exit 1

stop_server() {
  powershell.exe -NoProfile -Command \
    "Stop-Process -Name llama-server -Force -ErrorAction SilentlyContinue" >/dev/null 2>&1
  sleep 5
}

wait_healthy() {
  for _ in $(seq 1 120); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health 2>/dev/null)" = "200" ]; then
      echo "[$(date +%H:%M:%S)] server healthy"; return 0
    fi
    sleep 5
  done
  echo "[$(date +%H:%M:%S)] SERVER NEVER CAME UP"; return 1
}

run_model() {
  local tag="$1" gguf="$2" ctx="$3" np="$4"
  echo "=============================================================="
  echo "[$(date +%H:%M:%S)] $tag: loading $gguf (ctx=$ctx slots=$np)"
  stop_server
  "$LLAMA" --model "$GGUF/$gguf" --n-gpu-layers 999 --flash-attn on --jinja \
           --host 127.0.0.1 --port 8080 --threads 8 \
           --ctx-size "$ctx" --parallel "$np" > "$LOG/server_$tag.log" 2>&1 &
  wait_healthy || return 1
  nvidia-smi --query-gpu=memory.used --format=csv,noheader
  # notext and notextf are both memory probes and both cheap (~70-120/min);
  # named and forced are the expensive reading arms. Probes first, because
  # they decide whether the reading arms mean anything.
  #
  # notextf runs SAMPLED (--temp 1.0) and everything else runs greedy. Greedy
  # on a probe the model has no signal for collapses to a single constant
  # answer whose accuracy is just the base rate of up-moves, which cannot
  # distinguish "no recall" from "weak recall". Sampling makes the answer
  # vary, so accuracy over 1,815 events (SE 1.2pp) is a real measurement.
  for arm in notext notextf named forced; do
    temp=0.0
    [ "$arm" = "notextf" ] && temp=1.0
    echo "[$(date +%H:%M:%S)] $tag/$arm starting (temp=$temp)"
    "$PY" research_local_gate.py run --model "$tag" --arm "$arm" \
          --slots "$np" --temp "$temp"
    echo "[$(date +%H:%M:%S)] $tag/$arm done"
  done
}

# phi-4 measured at 21.7GB with these numbers. Do not raise ctx without
# re-checking nvidia-smi: the ceiling is ~22.5GB once the desktop is counted.
run_model phi4  phi-4.Q8_0.gguf                26624 4
run_model qwen3 qwen3-30b-a3b.UD-Q4_K_XL.gguf  19968 3

stop_server
echo "[$(date +%H:%M:%S)] ALL_LOCAL_ARMS_COMPLETE"
