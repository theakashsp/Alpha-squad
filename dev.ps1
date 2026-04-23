<#
.SYNOPSIS
    RescueRoute — one-command dev launcher for Windows (PowerShell)

.DESCRIPTION
    Starts the FastAPI backend and Next.js frontend in separate windows.
    Optionally runs the Bengaluru simulation after the backend is healthy.

.EXAMPLE
    .\dev.ps1              # start backend + frontend
    .\dev.ps1 -Simulate    # also run the simulation after startup
    .\dev.ps1 -BackendOnly
    .\dev.ps1 -FrontendOnly
#>

param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [switch]$Simulate,
    [string]$Vehicle = "BLR-AMB-001",
    [float]$Speed    = 45,
    [switch]$Demo
)

$Root     = $PSScriptRoot
$Backend  = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"

function Write-Header($msg) {
    Write-Host ""
    Write-Host ("  " + "═" * 54) -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host ("  " + "═" * 54) -ForegroundColor Cyan
}

function Wait-Backend($maxSeconds = 60) {
    Write-Host "  Waiting for backend to be healthy…" -ForegroundColor Yellow
    $deadline = (Get-Date).AddSeconds($maxSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8000/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                Write-Host "  ✓ Backend healthy" -ForegroundColor Green
                return $true
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  ✗ Backend did not start within $maxSeconds s" -ForegroundColor Red
    return $false
}

Write-Header "🚑  RescueRoute Dev Launcher"

# ── Backend ──────────────────────────────────────────────────────────────────
if (-not $FrontendOnly) {
    Write-Host "  Starting FastAPI backend…" -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$Root'; uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000"
    ) -WindowStyle Normal
}

# ── Frontend ─────────────────────────────────────────────────────────────────
if (-not $BackendOnly) {
    Write-Host "  Starting Next.js frontend…" -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$Frontend'; npm run dev"
    ) -WindowStyle Normal
}

# ── Simulation ────────────────────────────────────────────────────────────────
if ($Simulate) {
    $healthy = Wait-Backend
    if ($healthy) {
        Write-Host ""
        Write-Host "  Seeding junction fixtures…" -ForegroundColor Cyan
        try {
            Invoke-RestMethod -Method POST -Uri "http://localhost:8000/api/signals/seed" | Out-Null
            Write-Host "  ✓ Junctions seeded" -ForegroundColor Green
        } catch {
            Write-Host "  ⚠ Seed failed (already seeded?)" -ForegroundColor Yellow
        }

        Write-Host ""
        $demoFlag = if ($Demo) { "--demo" } else { "" }
        Write-Host "  Launching simulation  vehicle=$Vehicle  speed=$Speed km/h  demo=$Demo" -ForegroundColor Cyan
        Start-Process powershell -ArgumentList @(
            "-NoExit",
            "-Command",
            "Set-Location '$Root'; uv run python simulate_blr.py --vehicle $Vehicle --speed $Speed $demoFlag"
        ) -WindowStyle Normal
    }
}

Write-Host ""
Write-Host "  Backend  → http://localhost:8000" -ForegroundColor Green
Write-Host "  API docs → http://localhost:8000/docs" -ForegroundColor Green
Write-Host "  Frontend → http://localhost:3000" -ForegroundColor Green
Write-Host ""
