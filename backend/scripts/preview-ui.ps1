# Keyless UI preview: boot the engine on :8010 with NO broker keys, a
# throwaway SQLite store and no Redis, serving frontend/dist for a look at
# the terminal. Exists because the paper engine (NSSM service) owns :8000
# and the real paper account on this box - a second keyed engine would be a
# second exit enforcer on the same account. KEYLESS=true (config.py) makes
# this process refuse every key the root .env declares (the default pair
# and every ALPACA_ACCOUNT_<NAME>_* pair), so the account registry is empty.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/preview-ui.ps1
#   (from backend/; .claude/launch.json wires it as "ui-preview")

$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $backend

# PowerShell cannot export an EMPTY variable ($env:X = "" deletes it), so
# the keys are refused inside the process: KEYLESS=true blanks them in
# Settings.validate_paper_lock and empties the account registry.
$env:KEYLESS = "true"
$env:TRADING_MODE = "paper"
$env:STRATEGIES_ENABLED = "false"
$env:SQLITE_FALLBACK = "true"
$store = Join-Path $env:TEMP "planetaria-ui-preview.db"
$env:DATABASE_URL = "sqlite+aiosqlite:///" + ($store -replace "\\", "/")
$env:REDIS_URL = "redis://127.0.0.1:1/0"   # unreachable on purpose -> in-memory

Set-Location $backend
& ".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8010
