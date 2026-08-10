# check.ps1 -- the local CI. Everything that must be green, in one command,
# with all failures reported at the end rather than stopping at the first.
#
# Usage: .\check.ps1          full: ruff, backend tests, frontend typecheck + tests
#        .\check.ps1 -Fast    lint + typecheck only (~15s; what the pre-commit hook runs)
#        .\check.ps1 -Paper   also rebuild the pead paper and fail if report.html moves
#                             (the byte-identical regression gate from research/README.md)
#
# Wired as a pre-commit hook via:  git config core.hooksPath .githooks
param([switch]$Fast, [switch]$Paper)
$root = $PSScriptRoot
$py = "$root\backend\.venv\Scripts\python.exe"
$failed = @()

Write-Host "[check] ruff (backend + research)" -ForegroundColor Yellow
& $py -m ruff check "$root\backend" "$root\research"
if ($LASTEXITCODE -ne 0) { $failed += "ruff" }

if (-not $Fast) {
    Write-Host "[check] backend tests" -ForegroundColor Yellow
    Push-Location "$root\backend"
    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { $failed += "backend tests" }
    Pop-Location
}

Push-Location "$root\frontend"
Write-Host "[check] frontend typecheck" -ForegroundColor Yellow
npm run typecheck
if ($LASTEXITCODE -ne 0) { $failed += "frontend typecheck" }
Write-Host "[check] frontend lint" -ForegroundColor Yellow
npm run lint
if ($LASTEXITCODE -ne 0) { $failed += "frontend lint" }
if (-not $Fast) {
    Write-Host "[check] frontend tests" -ForegroundColor Yellow
    npm test
    if ($LASTEXITCODE -ne 0) { $failed += "frontend tests" }
}
Pop-Location

if ($Paper) {
    # Tier-2 regression gate: research/README.md says report.html must rebuild
    # byte-identical between stage runs. Requires the study caches on disk.
    Write-Host "[check] paper regression gate" -ForegroundColor Yellow
    Push-Location "$root\research\pead-llm-gate"
    & $py scripts\build_report_data.py
    if ($LASTEXITCODE -ne 0) { $failed += "paper: build_report_data" }
    & $py scripts\build_paper.py
    if ($LASTEXITCODE -ne 0) { $failed += "paper: build_paper" }
    Pop-Location
    git -C $root diff --quiet -- research/pead-llm-gate/paper/report.html
    if ($LASTEXITCODE -ne 0) { $failed += "paper gate: report.html moved (diff it before trusting anything)" }
}

if ($failed.Count -gt 0) {
    Write-Host "[check] FAILED: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "[check] all green." -ForegroundColor Green
exit 0
