#!/usr/bin/env bash
# One-time, needs sudo: installs the auto-deploy timer for the live box.
#   sudo bash deploy/live/install-autodeploy.sh
set -e
user=${SUDO_USER:-$USER}; repo=$(cd "$(dirname "$0")/../.." && pwd)
for f in planetaria-deploy.service planetaria-deploy.timer; do
  sed "s#__USER__#$user#g; s#__REPO__#$repo#g" "$repo/deploy/live/$f" > "/etc/systemd/system/$f"
done
systemctl daemon-reload && systemctl enable --now planetaria-deploy.timer
systemctl list-timers planetaria-deploy.timer --no-pager
echo "auto-deploy armed: runs every 15 min, acts only 20:30-07:30 ET or weekends; pause with: touch $repo/.deploy-hold"
