# The ISOLATED LIVE SERVER, side by side with the paper stack from dev.ps1.
#
#   .\live.ps1              boot the live server on http://127.0.0.1:8001
#   .\live.ps1 -Check       print the resolved env and exit (no boot)
#
# What makes this process different from the paper one is decided by
# PROCESS ENV, not by editing .env (pydantic-settings gives process env
# priority over both .env files, so the shared .env — where the live keys
# already sit as ALPACA_ACCOUNT_LIVE_ROTH_* — needs no change):
#
#   TRADING_MODE=live_manual   isolated live mode; strategy plane never built
#   STRATEGIES_ENABLED=false   required by the boot lock (belt AND braces)
#   LIVE_ACCOUNT_NAME=live_roth  the one pinned account; no DB selection
#   DATABASE_URL=.../trader_live  its own DB — two enforcers never share state
#   REDIS_URL=redis://.../1       its own redis namespace
#
# Bound to 127.0.0.1 ON PURPOSE. The API has no auth; the paper service
# binds 0.0.0.0 for phone access, which is not acceptable for real money.
# Remote access to live goes through Tailscale (docs/live-server.md).
param([switch]$Check, [switch]$Force)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$port = 8001
$liveDb = "trader_live"

$env:TRADING_MODE = "live_manual"
$env:STRATEGIES_ENABLED = "false"
$env:LIVE_ACCOUNT_NAME = "live_roth"
$env:DATABASE_URL = "postgresql+asyncpg://trader:trader@localhost:5433/$liveDb"
$env:REDIS_URL = "redis://localhost:6380/1"
# Never set ALPACA_PAPER here: live mode derives it (false) and ignores env.
Remove-Item Env:ALPACA_PAPER -ErrorAction SilentlyContinue

if ($Check) {
    Write-Host "TRADING_MODE=$env:TRADING_MODE STRATEGIES_ENABLED=$env:STRATEGIES_ENABLED LIVE_ACCOUNT_NAME=$env:LIVE_ACCOUNT_NAME"
    Write-Host "DATABASE_URL=$env:DATABASE_URL"
    Write-Host "REDIS_URL=$env:REDIS_URL"
    exit 0
}

Write-Host "[live] infra (postgres:5433, redis:6380)..." -ForegroundColor Yellow
docker compose -f "$root\docker-compose.dev.yml" up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose failed - is Docker Desktop running?" }

# The live DB is created empty; the app auto-migrates on first connect
# (db/session.py fresh-DB path: create_all + stamp head).
Write-Host "[live] ensuring database '$liveDb' exists..." -ForegroundColor Yellow
$exists = docker compose -f "$root\docker-compose.dev.yml" exec -T db `
    psql -U trader -tAc "SELECT 1 FROM pg_database WHERE datname='$liveDb'"
if (-not ($exists -match "1")) {
    docker compose -f "$root\docker-compose.dev.yml" exec -T db psql -U trader -c "CREATE DATABASE $liveDb"
    if ($LASTEXITCODE -ne 0) { throw "could not create $liveDb" }
    Write-Host "[live] created $liveDb" -ForegroundColor Green
}

if (-not (Test-Path "$root\.env")) { throw "no .env - the live keys live there (ALPACA_ACCOUNT_LIVE_ROTH_*)" }

# ONE LIVE PROCESS PER ACCOUNT, EVER. If the always-on box already runs the
# live server, booting a second one here means two enforcers own copies of
# the same positions and both try to close them. Refuse unless the peer is
# unreachable (and even then: be sure it is actually down, not just off the
# tailnet). Override only with -Force when you have stopped the peer.
$peer = if ($env:LIVE_PEER_URL) { $env:LIVE_PEER_URL } else { "https://mikoyae-kojiki.tail6d5ddc.ts.net" }
try {
    $h = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "$peer/api/health").Content
    if ($h -match '"mode":"live_manual"') {
        if (-not $Force) {
            throw "a LIVE server is already running at $peer - refusing to start a second one (stop it first, or -Force if you are certain)"
        }
        Write-Host "[live] WARNING: -Force with a live peer at $peer - you now have TWO enforcers on one account" -ForegroundColor Red
    }
} catch [System.Net.WebException], [System.Net.Http.HttpRequestException] {
    Write-Host "[live] no live peer reachable at $peer (offline or not on the tailnet) - proceeding" -ForegroundColor Yellow
}

Write-Host "[live] booting LIVE server on http://127.0.0.1:$port (no --reload: a reload mid-exit is an enforcement gap)" -ForegroundColor Red
Start-Process powershell -WorkingDirectory "$root\backend" -ArgumentList "-NoExit", "-Command",
    "`$env:TRADING_MODE='live_manual'; `$env:STRATEGIES_ENABLED='false'; `$env:LIVE_ACCOUNT_NAME='live_roth'; " +
    "`$env:DATABASE_URL='$env:DATABASE_URL'; `$env:REDIS_URL='$env:REDIS_URL'; Remove-Item Env:ALPACA_PAPER -ErrorAction SilentlyContinue; " +
    "& .\.venv\Scripts\Activate.ps1; `$host.UI.RawUI.WindowTitle='planetaria LIVE :$port'; " +
    "uvicorn app.main:app --host 127.0.0.1 --port $port"

Write-Host "[live] up. Terminal: http://127.0.0.1:$port/terminal.html  (needs a frontend build: cd frontend; npm run build)" -ForegroundColor Green
Write-Host "[live] the header badge must read LIVE in red. If it reads PAPER, you are on the wrong port." -ForegroundColor Yellow
