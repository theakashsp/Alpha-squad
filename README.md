# 🚑 RescueRoute — Rolling Green Wave for Bengaluru

> A multimodal AI platform that orchestrates a **"Rolling Green Wave"** for ambulances, clearing traffic signals 30 seconds ahead of arrival using vision + acoustic sensor fusion.

---

## Architecture Overview

```
┌──────────────┐        WebSocket        ┌──────────────────┐
│  AI Engine   │ ──── fusion events ───► │  FastAPI Backend  │
│  (YOLO-26 +  │                         │  (green_wave.py)  │
│   YAMNet)    │                         │  PostGIS queries  │
└──────────────┘                         └────────┬─────────┘
                                                  │ WebSocket
                                         ┌────────▼─────────┐
                                         │  Next.js Frontend │
                                         │  Mappls Maps SDK  │
                                         │  Live Signal HUD  │
                                         └──────────────────┘
```

## Directory Structure

```
Alpha-squad/
├── backend/            # FastAPI + SQLModel + PostGIS
│   ├── database.py     # Neon async engine + session factory
│   ├── models.py       # TrafficLight, Ambulance SQLModel tables
│   ├── manager.py      # WebSocket ConnectionManager
│   ├── green_wave.py   # ST_DWithin radius queries + Mappls ETA
│   └── main.py         # FastAPI app + routers
├── ai_engine/          # Inference pipeline
│   ├── vision.py       # YOLO-26 NMS-free ambulance detection
│   ├── acoustics.py    # YAMNet siren frequency identification
│   └── fusion.py       # Confidence-gated sensor fusion
├── frontend/           # Next.js 15 App Router
│   └── src/
│       ├── app/        # Root layout, page
│       ├── components/ # MapplsMap, Dashboard
│       └── hooks/      # useRescueStream (WebSocket)
├── simulate_blr.py     # Silk Board → Manipal Hospital simulation
├── pyproject.toml      # Python deps (managed with uv)
├── .env.example        # Environment variable template
└── README.md
```

## Quick Start

### Backend

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Run backend
uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Simulation

```bash
uv run python simulate_blr.py
```

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|---|---|
| `NEON_DATABASE_URL` | Neon pooled async connection string |
| `MAPPLS_API_KEY` | MapmyIndia / Mappls REST API key |
| `MAPPLS_SECRET` | Mappls OAuth secret |

## Key Thresholds

| Parameter | Value |
|---|---|
| Vision confidence gate | > 0.80 |
| Acoustic confidence gate | > 0.85 |
| Signal search radius | 500 m |
| Green-wave lead time | 30 s before junction |

---

*Built for the Bengaluru Smart City Emergency Response Initiative.*
