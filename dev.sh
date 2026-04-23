#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  RescueRoute — one-command dev launcher (Linux / macOS / WSL)
# ─────────────────────────────────────────────────────────────
# Usage:
#   ./dev.sh                   # backend + frontend
#   ./dev.sh --simulate        # also run the simulation
#   ./dev.sh --demo            # simulation in 5× demo mode
#   ./dev.sh --backend-only
#   ./dev.sh --frontend-only
# ─────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults
BACKEND=true
FRONTEND=true
SIMULATE=false
DEMO=""
VEHICLE="BLR-AMB-001"
SPEED=45

# Parse args
for arg in "$@"; do
  case $arg in
    --backend-only)   FRONTEND=false ;;
    --frontend-only)  BACKEND=false  ;;
    --simulate)       SIMULATE=true  ;;
    --demo)           DEMO="--demo"  ;;
    --vehicle=*)      VEHICLE="${arg#*=}" ;;
    --speed=*)        SPEED="${arg#*=}" ;;
  esac
done

_cyan()   { printf "\033[96m%s\033[0m\n" "$*"; }
_green()  { printf "\033[92m%s\033[0m\n" "$*"; }
_yellow() { printf "\033[93m%s\033[0m\n" "$*"; }
_bold()   { printf "\033[1m%s\033[0m\n"  "$*"; }

_bold ""
_bold "  ════════════════════════════════════════════════════"
_bold "  🚑  RescueRoute Dev Launcher"
_bold "  ════════════════════════════════════════════════════"

# ── Backend ─────────────────────────────────────────────────
if $BACKEND; then
  _cyan "  Starting FastAPI backend (port 8000)…"
  (cd "$ROOT" && uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000) &
  BACKEND_PID=$!
fi

# ── Frontend ─────────────────────────────────────────────────
if $FRONTEND; then
  _cyan "  Starting Next.js frontend (port 3000)…"
  (cd "$ROOT/frontend" && npm run dev) &
  FRONTEND_PID=$!
fi

# ── Wait for backend health ───────────────────────────────────
wait_backend() {
  local max=60 i=0
  _yellow "  Waiting for backend to be healthy…"
  while [ $i -lt $max ]; do
    if curl -sf "http://localhost:8000/health" > /dev/null 2>&1; then
      _green "  ✓ Backend healthy"
      return 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  _yellow "  ✗ Backend did not start within ${max}s"
  return 1
}

# ── Simulation ────────────────────────────────────────────────
if $SIMULATE; then
  wait_backend
  _cyan "  Seeding junction fixtures…"
  curl -sf -X POST http://localhost:8000/api/signals/seed > /dev/null && \
    _green "  ✓ Junctions seeded" || _yellow "  ⚠ Seed failed (may already exist)"

  _cyan "  Launching simulation  vehicle=${VEHICLE}  speed=${SPEED}  demo=${DEMO:-off}"
  (cd "$ROOT" && uv run python simulate_blr.py \
      --vehicle "$VEHICLE" \
      --speed "$SPEED" \
      $DEMO) &
fi

printf "\n"
_green "  Backend  → http://localhost:8000"
_green "  API docs → http://localhost:8000/docs"
_green "  Frontend → http://localhost:3000"
printf "\n"

# Keep script alive so Ctrl+C kills all children
trap 'kill $(jobs -p) 2>/dev/null' EXIT
wait
