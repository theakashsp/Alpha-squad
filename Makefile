# ─────────────────────────────────────────────────────────────
#  RescueRoute — Developer Makefile
# ─────────────────────────────────────────────────────────────
.PHONY: install install-fe install-be backend frontend dev sim sim-demo \
        seed lint fmt test clean

PYTHON   := uv run python
UVICORN  := uv run uvicorn
VEHICLE  ?= BLR-AMB-001
SPEED    ?= 45

# ── Install ──────────────────────────────────────────────────
install: install-be install-fe   ## Install all dependencies

install-be:                       ## Install Python deps via uv
	uv sync

install-fe:                       ## Install Node deps
	cd frontend && npm install

# ── Run ──────────────────────────────────────────────────────
backend:                          ## Start FastAPI backend (port 8000)
	$(UVICORN) backend.main:app --reload --host 0.0.0.0 --port 8000

frontend:                         ## Start Next.js frontend (port 3000)
	cd frontend && npm run dev

dev:                              ## Start both (requires tmux or parallel)
	@echo "Run 'make backend' and 'make frontend' in separate terminals."
	@echo "Or on Linux/macOS: ./dev.sh"
	@echo "On Windows:        .\\dev.ps1"

# ── Simulation ───────────────────────────────────────────────
seed:                             ## Seed Bengaluru junction fixtures
	curl -s -X POST http://localhost:8000/api/signals/seed | python -m json.tool

sim:                              ## Run Silk Board → Manipal simulation
	$(PYTHON) simulate_blr.py --vehicle $(VEHICLE) --speed $(SPEED)

sim-demo:                         ## Run simulation in 5× demo mode
	$(PYTHON) simulate_blr.py --vehicle $(VEHICLE) --speed $(SPEED) --demo

sim-dry:                          ## Print route ticks without connecting
	$(PYTHON) simulate_blr.py --dry-run --speed $(SPEED)

# ── Quality ──────────────────────────────────────────────────
lint:                             ## Lint Python with ruff
	uv run ruff check backend/ ai_engine/ simulate_blr.py

fmt:                              ## Format Python with ruff
	uv run ruff format backend/ ai_engine/ simulate_blr.py

type-check-fe:                    ## TypeScript type-check
	cd frontend && npm run type-check

test:                             ## Run Python tests
	uv run pytest -v

# ── Utilities ────────────────────────────────────────────────
clean:                            ## Remove caches and build artefacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf frontend/.next frontend/out
	@echo "Clean complete."

health:                           ## Check backend health endpoint
	curl -s http://localhost:8000/health | python -m json.tool

help:                             ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
