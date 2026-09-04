#!/usr/bin/env bash
# Idempotent setup of the PAPER server on the box, from its OWN checkout:
#   git clone https://github.com/matthewguoo/planetaria.git ~/planetaria-paper
#   cd ~/planetaria-paper && bash deploy/paper/setup-paper.sh
# Assumes deploy/live/setup-linux.sh already ran on this box (apt packages,
# postgres role trader, redis, node, logind no-sleep, tailscale).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USER_NAME="$(id -un)"
say() { printf '\n\033[1;33m[paper-setup]\033[0m %s\n' "$*"; }

say "1/6 postgres database trader (owner trader)"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='trader'" | grep -q 1; then
  sudo -u postgres psql -c "CREATE DATABASE trader OWNER trader"
  say "  created"
else
  say "  exists"
fi

# Re-runs skip the slow, already-done work (a fresh npm ci is minutes of
# silence on this box and reads as a hang). REBUILD=1 forces both.
say "2/6 backend venv"
cd "$REPO/backend"
if [ -x .venv/bin/uvicorn ] && [ -z "${REBUILD:-}" ]; then
  say "  exists (REBUILD=1 to reinstall)"
else
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.lock.txt
fi

say "3/6 frontend build (served by the backend; no node at runtime)"
cd "$REPO/frontend"
if [ -f dist/terminal.html ] && [ -d node_modules ] && [ -z "${REBUILD:-}" ]; then
  say "  built (REBUILD=1 to rebuild)"
else
  [ -d node_modules ] || npm ci
  npm run build
fi
cd "$REPO"

say "4/6 env file"
sudo mkdir -p /etc/planetaria
if [ ! -f /etc/planetaria/paper.env ]; then
  sudo install -m 640 -o root -g "$USER_NAME" "$REPO/deploy/paper/paper.env.example" /etc/planetaria/paper.env
  say "  created /etc/planetaria/paper.env - FILL THE PAPER KEYS (PK...) + FINNHUB before starting"
else
  say "  exists, left untouched"
fi
sudo chmod 640 /etc/planetaria/paper.env
mkdir -p "$HOME/planetaria-logs/paper" "$HOME/paper-store"

say "5/6 systemd unit + sudoers"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/deploy/paper/planetaria-paper.service" | sudo tee /etc/systemd/system/planetaria-paper.service >/dev/null
sed -e "s|__USER__|$USER_NAME|g" "$REPO/deploy/paper/sudoers-planetaria-paper" > /tmp/sudoers-planetaria-paper
sudo visudo -cf /tmp/sudoers-planetaria-paper >/dev/null
sudo install -m 440 -o root -g root /tmp/sudoers-planetaria-paper /etc/sudoers.d/planetaria-paper
rm -f /tmp/sudoers-planetaria-paper
sudo systemctl daemon-reload
sudo systemctl enable planetaria-paper
say "  enabled (not started)"

say "6/6 done"
cat <<EOF

Remaining MANUAL steps (keys, your login, the cutover - see docs/paper-server.md):

  1. sudo nano /etc/planetaria/paper.env      # paste PK keys (both pairs) + FINNHUB_API_KEY
  2. curl -fsSL https://claude.ai/install.sh | bash && claude     # login once as $USER_NAME
     claude -p "reply ok" --output-format json                    # proves the subscription auth
  3. Cutover: freeze the Windows paper engine, copy its trader.db snapshot to
     $HOME/paper-store/trader.db, then from $REPO/backend:
       .venv/bin/python -m app.db.copy_store --dry-run
       .venv/bin/python -m app.db.copy_store
       .venv/bin/python -m app.db.copy_store --verify
  4. sudo systemctl start planetaria-paper
     curl -s http://127.0.0.1:8000/api/health   # expect "mode":"paper","paper":true
  5. tailscale serve --bg --http=8000 http://127.0.0.1:8000   # then http://planetaria:8000/terminal
  6. sudo bash deploy/live/install-autodeploy.sh paper          # nightly self-deploy for this checkout
EOF
