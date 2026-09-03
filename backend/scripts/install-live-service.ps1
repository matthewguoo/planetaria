# Installs the ISOLATED LIVE SERVER as its own Windows service (NSSM), fully
# separate from planetaria-engine (the paper service): its own process, port,
# database, redis namespace and log files. Mirrors install-engine-service.ps1
# with two deliberate deltas:
#
#   1. the mode/isolation env is set on the SERVICE (AppEnvironmentExtra), so
#      the shared .env is untouched and the live keys stay where they are;
#   2. it binds 127.0.0.1 — the API has no auth, and a live-money API on the
#      LAN is not acceptable. Remote access goes through Tailscale
#      (docs/live-server.md).
#
#   RUN FROM AN ELEVATED POWERSHELL:
#     powershell -ExecutionPolicy Bypass -File backend\scripts\install-live-service.ps1
#
# The live DB (trader_live) must exist: run .\live.ps1 once first (it creates
# it), or CREATE DATABASE trader_live by hand.

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "This script must run in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Red
  exit 1
}

$repo = "C:\Users\matth\Desktop\planetaria"
$py = "$repo\backend\.venv\Scripts\python.exe"
$logs = "C:\Users\matth\AppData\Local\planetaria-logs"
$svc = "planetaria-live"
$port = 8001

# ET-window guard: never swap the live process while the enforcer may be
# mid-exit around the open/close.
$et = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), "Eastern Standard Time")
$hm = $et.Hour * 60 + $et.Minute
if (($hm -ge 565 -and $hm -le 600) -or ($hm -ge 945 -and $hm -le 965)) {
  Write-Host ("It is {0:HH:mm} ET - inside a protected window (09:25-10:00 / 15:45-16:05)." -f $et) -ForegroundColor Yellow
  $go = Read-Host "Type YES to proceed anyway"
  if ($go -ne "YES") { exit 1 }
}

$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
  Write-Host "Installing NSSM via winget..."
  winget install --id NSSM.NSSM --accept-source-agreements --accept-package-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "User")
  $nssm = Get-Command nssm -ErrorAction Stop
}

$cred = Get-Credential -UserName "$env:COMPUTERNAME\$env:USERNAME" -Message "Password for the service logon (your Windows account)"
$plain = $cred.GetNetworkCredential().Password

$existing = Get-Service $svc -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "Service exists - stopping and removing for a clean reinstall."
  nssm stop $svc | Out-Null
  nssm remove $svc confirm | Out-Null
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match "uvicorn app.main:app" -and $_.CommandLine -match "--port $port" } |
  ForEach-Object {
    Write-Host "Stopping existing live server pid $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
  }
Start-Sleep -Seconds 3

New-Item -ItemType Directory -Force $logs | Out-Null
nssm install $svc $py "-m uvicorn app.main:app --host 127.0.0.1 --port $port"
nssm set $svc AppDirectory "$repo\backend"
nssm set $svc AppStdout "$logs\live.out.log"
nssm set $svc AppStderr "$logs\live.err.log"
nssm set $svc AppRotateFiles 1
nssm set $svc AppRotateBytes 10485760
nssm set $svc AppExit Default Restart
nssm set $svc AppRestartDelay 5000
nssm set $svc Start SERVICE_AUTO_START
nssm set $svc ObjectName $cred.UserName $plain
nssm set $svc Description "planetaria LIVE server (uvicorn 127.0.0.1:$port, manual entries only, real money)"
# The isolation contract, on the service itself. Never ALPACA_PAPER: live
# mode derives it false and ignores env.
nssm set $svc AppEnvironmentExtra `
  "TRADING_MODE=live_manual" `
  "STRATEGIES_ENABLED=false" `
  "LIVE_ACCOUNT_NAME=live_roth" `
  "DATABASE_URL=postgresql+asyncpg://trader:trader@localhost:5433/trader_live" `
  "REDIS_URL=redis://localhost:6380/1"

nssm start $svc
Start-Sleep -Seconds 12
try {
  $health = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/health").Content
  Write-Host "HEALTH: $health" -ForegroundColor Green
  if ($health -notmatch '"mode":"live_manual"') {
    Write-Host "WARNING: health does not report mode=live_manual - check $logs\live.err.log" -ForegroundColor Red
  } else {
    Write-Host "`nDone. The LIVE server starts at boot and restarts on crash." -ForegroundColor Green
    Write-Host "Terminal (this box only): http://127.0.0.1:$port/terminal.html"
  }
} catch {
  Write-Host "Health check failed: $_" -ForegroundColor Red
  Write-Host "Check $logs\live.err.log and 'nssm status $svc'."
}
