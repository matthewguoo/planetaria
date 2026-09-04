#!/usr/bin/env bash
# Auto-deploy for the LIVE box, run by planetaria-deploy.timer OUTSIDE
# trading hours. "Latest working commit" means: origin/main, AND the full
# backend suite passes ON THIS BOX at that commit, AND the service comes
# back healthy - otherwise the checkout rolls back to the commit that was
# running and the service is restarted on it. Nothing here runs during
# the session: a restart mid-exit is an enforcement gap.
#
#   bash deploy/live/autodeploy.sh            # what the timer runs
#   bash deploy/live/autodeploy.sh --dry-run  # decide, print, change nothing
#   touch ~/planetaria/.deploy-hold           # pause auto-deploys (rm to resume)
set -u
REPO="${PLANETARIA_REPO:-$HOME/planetaria}"
LOG="${PLANETARIA_DEPLOY_LOG:-$HOME/planetaria-deploy.log}"
HEALTH="http://127.0.0.1:8001/api/health"
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
say() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }

# 1. Trading-hours guard, in ET. The engine may be mid-exit 09:25-10:00
#    and 15:45-16:05; extended-hours polling runs 04:00-20:00. Deploy only
#    in the quiet band 20:30-07:30 ET, or any time on Sat/Sun.
now_et=$(TZ=America/New_York date +%u:%H%M)   # 1-7:HHMM
dow=${now_et%%:*}; hhmm=${now_et##*:}
if (( dow <= 5 )) && [[ "$hhmm" > "0730" && "$hhmm" < "2030" ]]; then
  say "skip: inside trading day ($hhmm ET)"; exit 0
fi
[[ -f "$REPO/.deploy-hold" ]] && { say "skip: .deploy-hold present"; exit 0; }

cd "$REPO" || { say "FAIL: no repo at $REPO"; exit 1; }
git fetch -q origin main || { say "FAIL: git fetch"; exit 1; }
old=$(git rev-parse HEAD); new=$(git rev-parse origin/main)
[[ "$old" == "$new" ]] && { say "up to date at ${old:0:7}"; exit 0; }
if [[ -n "$(git status --porcelain)" ]]; then say "FAIL: dirty checkout - refusing"; exit 1; fi
say "candidate ${old:0:7} -> ${new:0:7}: $(git log --oneline -1 origin/main)"
(( DRY )) && { say "dry-run: would deploy"; exit 0; }

# 2. Switch, install, prove.
rollback() {
  say "ROLLBACK to ${old:0:7}: $1"
  git checkout -q "$old" && git checkout -q -B main "$old" 2>/dev/null
  (cd frontend && npm run build --silent >/dev/null 2>&1)
  sudo -n systemctl restart planetaria-live; sleep 12
  curl -sf --max-time 5 "$HEALTH" >/dev/null && say "rollback healthy" || say "ROLLBACK UNHEALTHY - human needed"
  exit 1
}
git merge -q --ff-only origin/main || { say "FAIL: not fast-forward"; exit 1; }
if git diff --name-only "$old" "$new" | grep -q '^backend/requirements.lock.txt$'; then
  backend/.venv/bin/pip install -q -r backend/requirements.lock.txt || rollback "pip install failed"
fi
if git diff --name-only "$old" "$new" | grep -q '^frontend/package-lock.json$'; then
  (cd frontend && npm ci --silent) || rollback "npm ci failed"
fi
# The suite is the definition of "working": ~1 minute on this box.
if ! (cd backend && .venv/bin/python -m pytest -q -p no:cacheprovider -x >>"$LOG.pytest" 2>&1); then
  rollback "backend tests failed at ${new:0:7} (see $LOG.pytest)"
fi
(cd frontend && npm run build --silent >/dev/null 2>&1) || rollback "frontend build failed"
sudo -n systemctl restart planetaria-live; sleep 12
if curl -sf --max-time 5 "$HEALTH" | grep -q '"mode":"live_manual"'; then
  say "deployed ${new:0:7}, healthy"
else
  rollback "health check failed after restart"
fi
