# Adaptive Freight — Real-Time Logistics Optimization

Industry-style real-time freight consolidation platform built around an event-driven FastAPI engine, asynchronous OSRM routing, dynamic multi-warehouse consolidation, live vehicle movement, disruption recovery, persistence, WebSocket state streaming, and production deployment scaffolding.

## What it does

- Continuously ingests shipments.
- Treats unused capacity on active vehicles as a reusable resource.
- Evaluates active vehicles before dispatching new vehicles.
- Can insert a new warehouse pickup and delivery into an existing live route.
- Uses fast local optimization first; OSRM road routing happens asynchronously.
- Draws real road geometry on a Leaflet/OpenStreetMap map.
- Gives every active vehicle a distinct route color.
- Moves vehicles along route geometry.
- Re-queues and reassigns shipments when a vehicle breaks down.
- Persists engine checkpoints and deduplicates external order submissions.
- Exposes /orders, /state, /health, /ready, /metrics, control endpoints, and a WebSocket stream.

## Run locally

PowerShell:

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    python run.py

Open:

    http://127.0.0.1:8000/

The production dashboard is served directly by FastAPI. Streamlit is optional and remains available through app.py.

## Real-time order cadence

The built-in live feed is independent of the simulation-speed slider:

    LIVE_ORDER_STREAM=true
    LIVE_ORDER_INTERVAL_S=25

To change to exactly one synthetic order every 15 real seconds:

    $env:LIVE_ORDER_INTERVAL_S=15
    python run.py

You can also change it live from the dashboard or with:

    POST /control/live-stream-interval/15

## Routing

Development:

    ROUTER_URL=https://router.project-osrm.org

Production recommendation:

    ROUTER_URL=http://127.0.0.1:5000

Routes are explicitly marked real or fallback. A fallback is never presented as a real road route.

Private OSRM preparation is documented in osrm/README.md and scripts/prepare_osrm.ps1.

## Architecture

    Live orders
        ↓
    FastAPI event engine
        ↓
    Fast optimizer
        ↓
    Active-route consolidation OR smallest-feasible dispatch
        ↓
    Async OSRM routing
        ↓
    Fleet state
        ↓
    WebSocket
        ↓
    Live colored map

Optional production infrastructure:

    Redis Streams
    PostgreSQL
    Prometheus
    Private OSRM
    Docker / Gunicorn

## External order API

Example endpoint:

    POST /orders

with JSON:

    {
      "shipment_id": "LIVE-001",
      "pickup_city": "DAL",
      "delivery_city": "HOU",
      "weight_kg": 3500,
      "revenue_usd": 2400,
      "priority": "Express"
    }

The same shipment ID can safely be retried because order ingestion is idempotent.

## Security

Set API_KEYS before exposing the API beyond localhost:

    API_KEYS=sk_admin:admin,sk_dispatch:dispatcher,sk_view:readonly

Roles are readonly, dispatcher, and admin.

## Production deployment

The repository includes:

- Dockerfile
- docker-compose.yml
- private OSRM scripts
- health/readiness endpoints
- Prometheus metrics
- PostgreSQL persistence
- optional Redis event streaming
- automated tests
- benchmark script
- Windows-safe launcher and port-conflict handling

Generated demo orders are not committed because the engine recreates data/order_stream.csv automatically when needed.

## Important limitation

This is a portfolio/engineering prototype, not a production TMS. Real logistics deployment still needs actual GPS/ELD telemetry, a routing provider with an appropriate SLA, TMS/WMS/ERP/EDI integration, driver workflows, multi-tenant identity, stronger distributed coordination, and operational controls.
