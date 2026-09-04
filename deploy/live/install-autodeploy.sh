#!/usr/bin/env bash
# One-time, needs sudo: installs the auto-deploy timer for one engine.
#   sudo bash deploy/live/install-autodeploy.sh          # live (default)
#   sudo bash deploy/paper/../live/install-autodeploy.sh paper   # from the paper checkout
# The paper checkout installs planetaria-paper-deploy.* from deploy/paper/;
# both run deploy/live/autodeploy.sh of THEIR OWN checkout with env overrides.
set -e
which=${1:-live}
user=${SUDO_USER:-$USER}; repo=$(cd "$(dirname "$0")/../.." && pwd)
home=$(getent passwd "$user" | cut -d: -f6)
case "$which" in
  live)  dir=live;  units="planetaria-deploy.service planetaria-deploy.timer"; timer=planetaria-deploy.timer ;;
  paper) dir=paper; units="planetaria-paper-deploy.service planetaria-paper-deploy.timer"; timer=planetaria-paper-deploy.timer ;;
  *) echo "usage: install-autodeploy.sh [live|paper]"; exit 1 ;;
esac
for f in $units; do
  sed "s#__USER__#$user#g; s#__REPO__#$repo#g; s#__HOME__#$home#g" "$repo/deploy/$dir/$f" > "/etc/systemd/system/$f"
done
systemctl daemon-reload && systemctl enable --now "$timer"
systemctl list-timers "$timer" --no-pager
echo "auto-deploy armed for $which: every 15 min, acts only 20:30-07:30 ET or weekends; pause with: touch $repo/.deploy-hold"
