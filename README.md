# Adaptive Freight — Real-Time Logistics Optimization

Industry-style real-time freight consolidation platform built around an event-driven FastAPI engine, asynchronous OSRM routing, nearest-warehouse allocation, package weight/volume constraints, active-route consolidation, 2-hour outbound batching, live driver notifications, disruption recovery, WebSocket state streaming, and production deployment scaffolding.

## Operating model

The live stress stream is intentionally set to **one new shipment every 15 seconds = 240 orders/hour**.

Each order contains:

- pickup/request city
- delivery city
- weight
- package volume
- package length / width / height
- priority
- pickup and delivery deadlines

The engine maps the request to its **nearest warehouse/hub**.

### Decision sequence

1. **Active truck check**
   - Search trucks that are already enroute.
   - Check remaining weight and volume capacity.
   - Check whether the truck can visit the order's warehouse and still reach the delivery within route-distance / detour limits.
   - If feasible, insert the warehouse pickup and delivery into that truck's route.
   - A driver notification is emitted with the warehouse, package size, added kilometres, pickup ETA, and estimated cost saving.

2. **Warehouse queue**
   - If no active truck can take the order, the shipment waits at its nearest warehouse.
   - Orders accumulate by warehouse.

3. **Outbound departure**
   - A warehouse truck departs when its load is approximately full, or when the oldest queued shipment reaches **2 simulated hours**.
   - The smallest suitable available vehicle is chosen using weight, volume, and route-distance limits.
   - Multiple deliveries are grouped into the outbound route.

This creates the intended operational behavior:

    240 orders/hour
          ↓
    nearest warehouse
          ↓
    active-route consolidation?
       ↙             ↘
     YES              NO
      ↓                ↓
    notify          warehouse queue
    driver               ↓
                 full truck OR 2 hours
                         ↓
                    truck departure
                         ↓
                  multi-stop route

## Optimization metrics

The dashboard reports:

- baseline dedicated-shipping cost
- allocated optimized cost
- estimated cost saved and percentage reduction
- shipment time saved
- active-route consolidation count
- truck dispatches
- dispatches avoided versus a one-truck-per-order baseline
- warehouse queue depth
- fleet utilization
- optimizer p95 latency
- real-road route coverage

The baseline is an estimated **dedicated shipment** from the assigned warehouse to the destination using the lowest-cost vehicle that can carry the package. Optimized cost is allocated from the marginal detour for active-route consolidation or from the batched outbound trip for warehouse departures.

## Real-time map

The map uses Leaflet/OpenStreetMap and asynchronous OSRM road geometry.

Every active truck gets a distinct route colour. Warehouse pickup stops, delivery stops, truck position, route quality, and disruption status are shown live.

## Run locally

PowerShell:

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    python run.py

Open:

    http://127.0.0.1:8000/

## Live order cadence

The built-in live feed is independent of simulation speed:

    LIVE_ORDER_STREAM=true
    LIVE_ORDER_INTERVAL_S=15

That produces exactly **240 live orders per real hour**.

The interval can also be changed from the dashboard or with:

    POST /control/live-stream-interval/15

## Routing

Development:

    ROUTER_URL=https://router.project-osrm.org

Production recommendation:

    ROUTER_URL=http://127.0.0.1:5000

Routes are explicitly marked real or fallback. A fallback is never presented as a real road route.

## Architecture

    Live orders
        ↓
    Nearest warehouse allocation
        ↓
    Fast active-route optimizer
        ↓
    ┌───────────────────────────┐
    │ feasible active truck?   │
    └──────────────┬────────────┘
                   │
           yes     │     no
            ↓      │      ↓
      driver       │   warehouse
     notification  │     queue
            ↓      │      ↓
      route insert │   full OR 2h
                   │      ↓
                   │  batch departure
                   └──────┬──────
                          ↓
                    async OSRM route
                          ↓
                    fleet movement
                          ↓
                       WebSocket
                          ↓
                    live control tower

Optional infrastructure:

    Redis Streams
    PostgreSQL
    Prometheus
    Private OSRM
    Docker / Gunicorn

## External order API

Example:

    POST /orders

with:

    {
      "shipment_id": "LIVE-001",
      "pickup_city": "SAT",
      "delivery_city": "HOU",
      "weight_kg": 3500,
      "volume_m3": 19.4,
      "package_length_m": 3.1,
      "package_width_m": 1.4,
      "package_height_m": 1.1,
      "revenue_usd": 2400,
      "priority": "Express"
    }

The server automatically allocates the order to the nearest warehouse/hub.

The same shipment ID can safely be retried because order ingestion is idempotent.

## Security

Set API_KEYS before exposing the API beyond localhost:

    API_KEYS=sk_admin:admin,sk_dispatch:dispatcher,sk_view:readonly

Roles are readonly, dispatcher, and admin.

## Free Render deployment

The current portfolio setup uses:

    Render Free
        ↓
    FastAPI realtime backend
        ↓
    Streamlit Community Cloud frontend
        ↓
    OSRM

For the free Render filesystem, persistence should remain disabled:

    PERSISTENCE_ENABLED=false
    CHECKPOINT_INTERVAL_S=0

The project remains intentionally suitable for demonstration and portfolio use rather than claiming production TMS readiness.

## Advanced optimization capabilities

### Phase 1 — mathematical optimization
- Counterfactual traditional baseline for every shipment: dedicated truck distance, cost, service time, and delivery time.
- Adaptive-vs-traditional cost/time comparison at shipment and network levels.
- OSRM Table API is used in the realtime candidate filter for road travel time.
- SLA-aware pickup and delivery feasibility with explicit time buffers.
- Pickup, warehouse loading, handoff, and delivery service times.
- Empty-mile tracking and backhaul detection.

### Phase 2 — operational control tower
- Clickable warehouse markers with live orders, weight, volume, trucks, departure deadline, and forecast.
- Candidate incoming trucks with ETA and remaining capacity.
- Driver accept/reject workflow with automatic timeout fallback.
- Decision explanations including SLA buffer, detour, savings, and rejected-candidate reasons.
- Traditional-vs-adaptive shipment comparison table.

### Phase 3 — advanced planning
- Breakdown cargo transfer at the breakdown location to another feasible truck.
- Automatic demand forecasting by warehouse for the next hour.
- OR-Tools CP-SAT fleet-to-warehouse repositioning recommendations.
- Manual fleet reoptimization endpoint and dashboard control.
- Replayable event timeline with sequence numbers and simulation timestamps.

The fleet repositioning optimizer runs periodically in the background and can also be triggered through:

    POST /fleet/reoptimize

Driver decisions are exposed through:

    GET  /driver-offers
    POST /driver-offers/{offer_id}

Shipment comparison is exposed through:

    GET /shipments/{shipment_id}

Event replay is exposed through:

    GET /replay


## Oracle Cloud deployment

The repository includes an OCI Always Free deployment stack with FastAPI, PostgreSQL, Redis, private ARM64 OSRM, Caddy HTTPS, persistent volumes, and automatic Docker restarts.

See [OCI_DEPLOYMENT.md](OCI_DEPLOYMENT.md).

## Important limitation

This is a portfolio/engineering prototype, not a production TMS. Real logistics deployment still needs GPS/ELD telemetry, WMS/TMS/ERP/EDI integration, carrier contracts and rates, driver workflows, persistent event storage, distributed coordination, SLA policies, security hardening, and a routing provider with an appropriate SLA.
