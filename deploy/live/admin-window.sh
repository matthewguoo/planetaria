#!/usr/bin/env bash
# Open one engine's administration window on the box's own screen: a
# chromeless browser app at http://127.0.0.1:PORT/admin (stats, the engine
# feed, every broker call), sized to half the screen so live and paper sit
# side by side. Runs as the desktop user; no sudo.
#
#   bash deploy/live/admin-window.sh 8001 left     # live
#   bash deploy/live/admin-window.sh 8000 right    # paper
#
# Each window has its own browser profile dir so two can be open at once,
# and it waits for the engine to answer /api/health before opening so a
# window never shows a connection error at login.
set -u
PORT="${1:-8001}"
SIDE="${2:-left}"
URL="http://127.0.0.1:${PORT}/admin"
PROFILE="$HOME/.config/planetaria-admin-${PORT}"
export DISPLAY="${DISPLAY:-:0}"

for b in google-chrome google-chrome-stable chromium chromium-browser; do
  if command -v "$b" >/dev/null 2>&1; then BROWSER="$b"; break; fi
done
if [ -z "${BROWSER:-}" ]; then echo "no chromium-family browser found" >&2; exit 1; fi

# Screen size (X11); fall back to a 1920x1080 assumption.
W=1920; H=1080
if command -v xdpyinfo >/dev/null 2>&1; then
  dims=$(xdpyinfo 2>/dev/null | awk '/dimensions:/ {print $2}')
  [ -n "$dims" ] && W=${dims%x*} && H=${dims#*x}
fi
HALF=$((W / 2))
X=0; [ "$SIDE" = "right" ] && X=$HALF

# Wait (up to 3 min) for the engine so the window opens onto a live page.
for _ in $(seq 1 90); do
  curl -sf --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null && break
  sleep 2
done

exec "$BROWSER" \
  --app="$URL" \
  --user-data-dir="$PROFILE" \
  --window-position="${X},0" \
  --window-size="${HALF},${H}" \
  --no-first-run --no-default-browser-check --disable-session-crashed-bubble \
  --autoplay-policy=no-user-gesture-required \
  >/dev/null 2>&1
