# Installs the planetaria engine as a Windows service (NSSM) so it starts at
# boot, survives logouts/app closes, and restarts on crash.
#
#   RUN FROM AN ELEVATED POWERSHELL:
#     powershell -ExecutionPolicy Bypass -File backend\scripts\install-engine-service.ps1
#
# Notes:
# - The service runs AS YOUR USER ACCOUNT (prompted below) so the venv, the
#   SQLite state, and the claude-cli subscription auth all keep working.
#   LocalSystem would silently break the LLM backend.
# - Binds 0.0.0.0 so the phone on the LAN can reach the terminal at
#   http://<this-box-ip>:8000/terminal.html. The API has NO auth — keep it
#   LAN-only (no port forwarding), paper keys only.
# - Avoid running the final swap 09:00-09:30 or 13:55-14:05 ET (gff scan /
#   fly entry windows).

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "This script must run in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Red
  exit 1
}

$repo = "C:\Users\matth\Desktop\planetaria"
$py = "$repo\backend\.venv\Scripts\python.exe"
$logs = "C:\Users\matth\AppData\Local\planetaria-logs"
$svc = "planetaria-engine"

# ET-window guard (box clock is Pacific; compute ET properly).
$et = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), "Eastern Standard Time")
$hm = $et.Hour * 60 + $et.Minute
if (($hm -ge 535 -and $hm -le 575) -or ($hm -ge 830 -and $hm -le 850)) {
  Write-Host ("It is {0:HH:mm} ET - inside a protected trading window (09:00-09:30 / 13:55-14:05)." -f $et) -ForegroundColor Yellow
  $go = Read-Host "Type YES to proceed anyway"
  if ($go -ne "YES") { exit 1 }
}

# 1. NSSM
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
  Write-Host "Installing NSSM via winget..."
  winget install --id NSSM.NSSM --accept-source-agreements --accept-package-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "User")
  $nssm = Get-Command nssm -ErrorAction Stop
}
Write-Host "nssm: $($nssm.Source)"

# 2. Your account credentials (service logon). Entered here, never stored.
$cred = Get-Credential -UserName "$env:COMPUTERNAME\$env:USERNAME" -Message "Password for the service logon (your Windows account)"
$plain = $cred.GetNetworkCredential().Password

# 3. Stop any existing engine (detached run-engine.cmd tree or old service).
$existing = Get-Service $svc -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "Service exists - stopping and removing for a clean reinstall."
  nssm stop $svc | Out-Null
  nssm remove $svc confirm | Out-Null
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match "uvicorn app.main:app" } |
  ForEach-Object {
    Write-Host "Stopping existing engine pid $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
  }
Start-Sleep -Seconds 3

# 4. Install + configure
New-Item -ItemType Directory -Force $logs | Out-Null
nssm install $svc $py "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
nssm set $svc AppDirectory "$repo\backend"
nssm set $svc AppStdout "$logs\engine.out.log"
nssm set $svc AppStderr "$logs\engine.err.log"
nssm set $svc AppRotateFiles 1
nssm set $svc AppRotateBytes 10485760
nssm set $svc AppExit Default Restart
nssm set $svc AppRestartDelay 5000
nssm set $svc Start SERVICE_AUTO_START
nssm set $svc ObjectName $cred.UserName $plain
nssm set $svc Description "planetaria trading engine (uvicorn :8000, paper-only)"

# 5. Start + health check
nssm start $svc
Start-Sleep -Seconds 12
try {
  $health = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/health).Content
  Write-Host "HEALTH: $health" -ForegroundColor Green
  Write-Host "`nDone. The engine now starts at boot and restarts on crash." -ForegroundColor Green
  Write-Host "Phone: http://$((Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -like '192.168.*'} | Select-Object -First 1).IPAddress):8000/terminal.html"
} catch {
  Write-Host "Health check failed: $_" -ForegroundColor Red
  Write-Host "Check $logs\engine.err.log and 'nssm status $svc'."
}
