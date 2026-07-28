# Hybrid dev boot: infra in Docker, apps native for fast hot reload.
# Usage: .\dev.ps1            (starts everything)
#        .\dev.ps1 -InfraOnly (just redis+postgres)
param([switch]$InfraOnly)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "[dev] starting infra (postgres:5433, redis:6380)..." -ForegroundColor Yellow
docker compose -f "$root\docker-compose.dev.yml" up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose failed - is Docker Desktop running?" }

if ($InfraOnly) { Write-Host "[dev] infra up." -ForegroundColor Green; exit 0 }

if (-not (Test-Path "$root\.env")) {
    Write-Host "[dev] WARNING: no .env file - copy .env.example and add Alpaca keys" -ForegroundColor Red
}

Write-Host "[dev] starting backend (http://localhost:8000)..." -ForegroundColor Yellow
Start-Process powershell -WorkingDirectory "$root\backend" -ArgumentList "-NoExit", "-Command",
    "& .\.venv\Scripts\Activate.ps1; uvicorn app.main:app --reload --port 8000"

Write-Host "[dev] starting frontend (http://localhost:5173)..." -ForegroundColor Yellow
Start-Process powershell -WorkingDirectory "$root\frontend" -ArgumentList "-NoExit", "-Command", "npm run dev"

Write-Host "[dev] all up. Backend :8000, Frontend :5173." -ForegroundColor Green
