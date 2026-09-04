#!/usr/bin/env bash
# Put the administration windows on the box's screen at every desktop login
# (XDG autostart; no sudo - runs as the desktop user). Live on the left,
# paper on the right. Re-running just rewrites the entries.
#
#   bash deploy/live/install-admin-window.sh            # both
#   bash deploy/live/install-admin-window.sh live       # live only
#   bash deploy/live/install-admin-window.sh --now      # also open them right now
set -eu
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUTOSTART="$HOME/.config/autostart"
mkdir -p "$AUTOSTART"
which_=all; now=0
for a in "$@"; do case "$a" in live|paper) which_=$a ;; --now) now=1 ;; esac; done

entry() {  # name port side
  cat > "$AUTOSTART/planetaria-admin-$1.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=planetaria admin ($1)
Comment=Server administration window for the $1 engine
Exec=/usr/bin/bash $REPO/deploy/live/admin-window.sh $2 $3
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
  echo "autostart: $AUTOSTART/planetaria-admin-$1.desktop -> :$2 ($3)"
}
[ "$which_" = all ] || [ "$which_" = live ] && entry live 8001 left
[ "$which_" = all ] || [ "$which_" = paper ] && entry paper 8000 right
if [ "$now" = 1 ]; then
  [ "$which_" = all ] || [ "$which_" = live ] && (nohup bash "$REPO/deploy/live/admin-window.sh" 8001 left >/dev/null 2>&1 &)
  [ "$which_" = all ] || [ "$which_" = paper ] && (nohup bash "$REPO/deploy/live/admin-window.sh" 8000 right >/dev/null 2>&1 &)
  echo "opened"
fi
