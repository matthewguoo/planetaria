#!/usr/bin/env bash
# One-shot, idempotent setup of the planetaria LIVE server on an always-on
# Debian/Ubuntu box. Run as the login user that will own the service (it
# uses sudo where root is needed). Re-running is safe.
#
#   cd ~/planetaria && bash deploy/live/setup-linux.sh
#
# What it does, in order (docs/live-server.md explains why):
#   1. apt: python3-venv, postgresql, redis-server, git, curl (+ node for the UI build)
#   2. postgres: role trader/trader and database trader_live
#   3. backend venv from requirements.lock.txt
#   4. frontend build (the live box serves frontend/dist itself)
#   5. /etc/planetaria/live.env from the example (keys left for you to fill)
#   6. systemd unit installed + enabled (NOT started until live.env has keys)
#   7. power: no sleep on lid close / idle
#   8. tailscale installed (`tailscale up` and `tailscale serve` are manual:
#      they need your browser login)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USER_NAME="$(id -un)"
PY="${PYTHON:-python3}"

say() { printf '\n\033[1;33m[live-setup] %s\033[0m\n' "$*"; }

say "1/8 packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip postgresql redis-server git curl
if ! command -v node >/dev/null 2>&1; then
  say "node not found - installing Node 20 (NodeSource) for the frontend build"
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
sudo systemctl enable --now postgresql redis-server

say "2/8 postgres role + trader_live database"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='trader'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE trader LOGIN PASSWORD 'trader'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='trader_live'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE DATABASE trader_live OWNER trader"

say "3/8 backend venv"
cd "$REPO/backend"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.lock.txt

say "4/8 frontend build (served by the backend at /terminal.html)"
cd "$REPO/frontend"
npm ci --silent
npm run build --silent

say "5/8 /etc/planetaria/live.env"
sudo mkdir -p /etc/planetaria
if [ ! -f /etc/planetaria/live.env ]; then
  sudo install -m 600 -o root -g "$USER_NAME" "$REPO/deploy/live/live.env.example" /etc/planetaria/live.env
  say "  created /etc/planetaria/live.env - FILL THE TWO ALPACA_ACCOUNT_LIVE_ROTH_* KEYS before starting"
else
  say "  exists, left untouched"
fi
# The service reads the file as the service user; root-owned + group-readable.
sudo chmod 640 /etc/planetaria/live.env

say "6/8 systemd unit"
mkdir -p "$HOME/planetaria-logs/live"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/deploy/live/planetaria-live.service" | sudo tee /etc/systemd/system/planetaria-live.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable planetaria-live
say "  enabled (start it after live.env has keys: sudo systemctl start planetaria-live)"

say "7/8 power: this box must never sleep on an open position"
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/planetaria.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
sudo systemctl restart systemd-logind || true
sudo timedatectl set-ntp true || true

say "8/8 tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
cat <<EOF

Done. Remaining MANUAL steps (need your browser / your keys):

  1. sudo nano /etc/planetaria/live.env         # paste the two AK... live keys
  2. sudo systemctl start planetaria-live
     curl -s http://127.0.0.1:8001/api/health   # expect "mode":"live_manual","paper":false
  3. sudo tailscale up                          # login link; join your tailnet
     sudo tailscale serve --bg --https=443 http://127.0.0.1:8001
     tailscale status                           # note this box's MagicDNS name
  4. From your Windows box: https://<this-box>.<tailnet>.ts.net/terminal.html
     The header badge must read LIVE in red.

  NEVER: tailscale funnel (public internet), --host 0.0.0.0, or a second
  live server on the same account (two enforcers = double exits).

Then run the verification tests in docs/live-server.md.
EOF
