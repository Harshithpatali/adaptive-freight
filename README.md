
# Adaptive Freight — Real-Time Logistics Optimization

> **An Operations Research, Applied Mathematics, and Data Science approach to dynamic freight consolidation, routing, and fleet repositioning under capacity, time-window, and operational constraints.**

Adaptive Freight is a real-time logistics optimization platform built around a difficult transportation question:

> **When a new shipment appears after vehicles have already started moving, should the shipment be inserted into an existing route, held at a warehouse for consolidation, transferred to another vehicle after a disruption, or served by a newly dispatched vehicle?**

The project treats freight movement as a **dynamic optimization problem** rather than a static shortest-path problem.

The system continuously observes orders, vehicle positions, route progress, warehouse queues, service times, capacity, demand estimates, disruptions, and delivery deadlines. Decisions combine:

- dynamic route-insertion heuristics
- weight and volume constraints
- road travel times from OSRM
- pickup and delivery time windows
- warehouse batching
- marginal route distance and cost
- empty-mile and backhaul analysis
- disruption recovery
- short-horizon demand-rate forecasting
- OR-Tools CP-SAT fleet repositioning
- event-driven simulation
- replayable operational history

The emphasis is not simply **find the shortest route**. It is the interaction between **geometry, combinatorial optimization, time, uncertainty, and operational decisions**.

---

# 1. Problem statement

A traditional transportation example often assumes that all shipments are known in advance, routes are planned once, travel times are deterministic, and vehicles operate from fixed depots.

Real freight networks do not behave this way.

Orders arrive continuously. Vehicles are already moving. New freight can appear away from the current route. A vehicle can have physical capacity but insufficient time to make another stop. A warehouse can accumulate enough freight to make consolidation attractive, but waiting too long can threaten service levels. A vehicle failure can invalidate an otherwise feasible plan.

The resulting problem is naturally modeled as a **dynamic capacitated routing and consolidation problem with time windows**.

At each decision epoch, the system asks:

$$
\text{Which feasible transportation action should be taken now?}
$$

Possible actions are:

- insert a shipment into an active route,
- queue it at a warehouse,
- assign another active vehicle,
- dispatch a warehouse vehicle,
- reposition an idle vehicle,
- transfer cargo after a breakdown,
- or defer the shipment until a later decision point.

---

# 2. Central logistics idea

Suppose an active truck is traveling:

$$
A \rightarrow C
$$

and a new shipment appears:

$$
B \rightarrow C
$$

A simple system may immediately dispatch another truck.

Adaptive Freight first tests whether:

$$
A \rightarrow B \rightarrow C
$$

is operationally feasible.

The truck must have enough remaining:

- weight capacity,
- volume capacity,

and the modified route must satisfy:

- route-distance limits,
- detour limits,
- pickup deadline,
- delivery deadline,
- pickup service time,
- delivery service time.

The economic condition is:

$$
\text{Incremental cost of consolidation}
<
\text{Dedicated-shipment baseline cost}
$$

subject to those constraints.

This is the core mathematical idea of the project.

---

# 3. Mathematical formulation

## 3.1 Sets and parameters

Let:

- $V$ = set of vehicles
- $S$ = set of shipments
- $W$ = set of warehouses
- $C$ = set of city/network nodes

For shipment $s$:

- $o_s$ = pickup location
- $d_s$ = delivery location
- $w_s$ = shipment weight
- $q_s$ = shipment volume
- $t_s^p$ = pickup deadline
- $t_s^d$ = delivery deadline
- $tau_s^p$ = pickup service time
- $tau_s^d$ = delivery service time
- $r_s$ = shipment revenue

For vehicle $v$:

- $Q_v$ = weight capacity
- $U_v$ = volume capacity
- $q_v(t)$ = current loaded weight
- $u_v(t)$ = current loaded volume
- $c_v$ = cost per kilometer
- $L_v$ = maximum route distance
- $z_v(t)$ = current position
- $nu_v$ = nominal speed

---

# 4. Capacity feasibility

A candidate shipment can be assigned only if both physical capacity constraints hold:

$$
q_v(t)+w_s\leq Q_v
$$

and:

$$
u_v(t)+q_s\leq U_v
$$

The model therefore treats weight and volume separately.

This is important because a vehicle can have spare weight capacity while being effectively full from a volume perspective.

---

# 5. Dynamic route insertion

Let the current active route of vehicle $v$ be:

$$
R_v=(z_v,r_1,r_2,\ldots,r_n)
$$

For a new shipment $s$, the optimizer considers feasible positions for:

1. the pickup stop,
2. the delivery stop.

A candidate route has the form:

$$
R'_v=(z_v,\ldots,o_s,\ldots,d_s,\ldots)
$$

with pickup occurring before delivery.

The incremental route distance is:

$$
\Delta D=D(R'_v)-D(R_v)
$$

The optimizer searches insertion positions and rejects candidates that violate route or detour limits.

This is a dynamic version of route insertion: the vehicle is **already moving**, so the problem is not solved from a clean depot state.

---

# 6. Detour constraint

For a sufficiently long existing route, the relative detour is:

$$
\delta=\frac{\Delta D}{D(R_v)}
$$

The implementation uses an explicit detour threshold to prevent the optimizer from accepting mathematically feasible but operationally disproportionate deviations.

This creates an important distinction:

$$
\text{Feasible} \neq \text{Operationally attractive}
$$

A truck having spare capacity is not sufficient justification for an arbitrarily large detour.

---

# 7. Time-window feasibility

A candidate must satisfy both pickup and delivery deadlines.

Estimated pickup duration:

$$
T_{pickup}
=
T_{current\rightarrow pickup}
+
tau_s^p
$$

Estimated delivery duration:

$$
T_{delivery}
=
T_{road}
+
tau_s^p
+
tau_s^d
$$

The feasibility inequalities are:

$$
t_{now}+T_{pickup}\leq t_s^p
$$

and:

$$
t_{now}+T_{delivery}\leq t_s^d
$$

This explicitly models loading and unloading time rather than pretending that a warehouse or destination takes zero time.

---

# 8. SLA buffer

For every feasible candidate the system calculates delivery slack:

$$
B_s=t_s^d-(t_{now}+T_{delivery})
$$

This is displayed as **SLA buffer in minutes**.

The buffer separates two plans that are both technically feasible:

- a plan with large schedule slack,
- a plan that reaches the deadline with almost no remaining buffer.

That difference matters in real operations because a small disturbance can turn a low-buffer plan into a late delivery.

---

# 9. Cost and counterfactual reasoning

For an active route, marginal transportation cost is approximated by:

$$
C_{incremental}=\Delta D\cdot c_v
$$

The dedicated baseline is:

$$
C_{baseline}=D_{baseline}\cdot c_{baseline}
$$

The estimated consolidation saving is:

$$
S_s=C_{baseline}-C_{incremental}
$$

The system therefore has a shipment-level counterfactual:

$$
\Delta C=C_{traditional}-C_{adaptive}
$$

and:

$$
\Delta T=T_{traditional}-T_{adaptive}
$$

This lets the project answer a more useful question than "which route did the optimizer choose?":

> **What changed because the optimizer was used?**

---

# 10. Multi-objective candidate score

The online optimizer uses an explicit, interpretable decision score.

Conceptually:

$$
Score(v,s)=
alpha S_s
-beta\Delta D
-gamma\delta
+eta B_s
+kappa I_{backhaul}
+lambda I_{priority}
$$

The concrete implementation combines:

- monetary saving,
- incremental kilometers,
- detour,
- SLA buffer,
- shipment priority,
- backhaul opportunity.

The score is an explicit decision function, not a learned black-box probability.

The advantage is interpretability: each term has a direct operational meaning.

---

# 11. Road-network routing

Straight-line distance is not enough for operational ETA.

The routing layer uses OSRM for road-network travel time and route geometry.

Candidate evaluation uses travel-time information corresponding to:

$$
T_{current\rightarrow pickup}
$$

and:

$$
T_{pickup\rightarrow delivery}
$$

Service time is then included:

$$
T_{shipment}
=
T_{road}
+
tau_{pickup}
+
tau_{delivery}
$$

The application tracks whether routing information came from a real OSRM route or a configured fallback.

The control tower exposes:

- routing requests,
- real route count,
- fallback route count,
- real-route percentage,
- route quality,
- routing error/circuit state.

---

# 12. Warehouse queueing

If no active truck can satisfy the constraints, the shipment is assigned to its nearest warehouse/hub and enters a queue.

For warehouse $w$:

$$
Q_w(t)=
\{s:\text{shipment }s\text{ waits at }w\}
$$

The queue tracks:

- number of orders,
- waiting weight,
- waiting volume,
- oldest wait,
- departure deadline,
- available vehicles,
- incoming candidate trucks,
- forecast demand.

The current operational rule is:

$$
T_{warehouse}^{max}=120\text{ simulated minutes}
$$

or departure when the queued load is approximately full.

This is an explicit service-vs-utilization trade-off:

$$
\text{More waiting}
\Rightarrow
\text{more consolidation}
$$

but:

$$
\text{More waiting}
\Rightarrow
\text{more SLA exposure}
$$

---

# 13. Empty miles and backhaul

Fleet efficiency cannot be measured only by loaded distance.

The system separates:

$$
D_{total}=D_{loaded}+D_{empty}
$$

and reports:

$$
Empty\ Mile\ Ratio=
\frac{D_{empty}}{D_{total}}
$$

Backhaul opportunities are also identified and tracked.

Metrics include:

- empty kilometers,
- empty-kilometer percentage,
- backhaul shipment count,
- backhaul kilometers saved,
- repositioning kilometers.

This provides a fleet-level perspective:

> The objective is not just to reduce the distance of one shipment, but to improve the utilization of total transportation capacity.

---

# 14. Breakdown recovery

A vehicle breakdown transforms the routing problem into a recovery problem.

If vehicle $v_1$ fails at location $b$:

$$
v_1\rightarrow b\rightarrow\text{breakdown}
$$

the engine can mark $b$ as a handoff point and search for an alternative feasible vehicle.

The replacement must still satisfy:

- weight capacity,
- volume capacity,
- route constraints,
- time-window constraints.

The recovery process is:

$$
\text{broken vehicle}
\rightarrow
\text{cargo handoff}
\rightarrow
\text{replacement vehicle}
\rightarrow
\text{continued delivery}
$$

The event is recorded for later replay.

---

# 15. Demand-rate forecasting

The project contains a lightweight online forecasting model for warehouse demand.

Recent arrivals are maintained over a rolling observation window.

If $N_w$ orders are observed over $Delta t$:

$$
hat{lambda}_w=\frac{N_w}{Delta t}
$$

For horizon $H$:

$$
hat{D}_w(H)=hat{lambda}_w H
$$

Average recent weight and volume are projected in the same way.

The forecast returns:

- order forecast,
- weight forecast,
- volume forecast,
- confidence.

This is deliberately a transparent **rate-based forecast**, not a trained machine-learning model.

It exists to demonstrate how statistical estimation can feed directly into a mathematical planning decision.

---

# 16. Global fleet repositioning with OR-Tools

Forecasts become useful when they influence idle-vehicle placement.

Let:

$$
x_{vw}=
\begin{cases}
1,&\text{vehicle }v\text{ is sent to warehouse }w\\
0,&\text{otherwise}
\end{cases}
$$

A simplified objective is:

$$
max\sum_{v,w}
( rho D_w-gamma d_{vw})x_{vw}
$$

where:

- $D_w$ = forecast demand at warehouse $w$
- $d_{vw}$ = distance from vehicle $v$ to warehouse $w$
- $rho$ = demand reward
- $gamma$ = repositioning penalty

Vehicle assignment constraint:

$$
\sum_w x_{vw}\le1
\qquad\forall v
$$

So an idle vehicle can be assigned to at most one destination in each planning cycle.

The implementation solves this small combinatorial problem using **OR-Tools CP-SAT** with a short time budget.

---

# 17. Why there are two optimization layers

Trying to solve one large exact dynamic VRP after every incoming order would be unnecessary and computationally expensive.

The project separates decisions by timescale.

## Local online optimization

For a new shipment:

$$
\text{Find the best feasible active truck}
$$

This requires low latency.

## Global periodic optimization

Every few simulated minutes:

$$
\text{Where should idle fleet capacity be positioned?}
$$

This is solved with a dedicated CP-SAT model.

The architecture is therefore:

$$
\boxed{
\text{fast local route decisions}
+
\text{periodic global fleet planning}
}
$$

This is a deliberate Operations Research design trade-off.

---

# 18. Event-driven simulation

The system behaves like a discrete-event simulator.

Events include:

- order arrival,
- warehouse allocation,
- route insertion,
- driver offer,
- driver accept/reject,
- pickup,
- loading,
- delivery,
- warehouse dispatch,
- vehicle breakdown,
- cargo transfer,
- vehicle repair,
- fleet repositioning,
- periodic global optimization.

The dynamic system state can be viewed as:

$$
X_t=
(
V_t,
S_t,
Q_t,
R_t,
F_t,
M_t
)
$$

where:

- $V_t$ = vehicle state
- $S_t$ = shipment state
- $Q_t$ = warehouse queues
- $R_t$ = routes
- $F_t$ = forecasts
- $M_t$ = metrics

An event creates:

$$
X_t\xrightarrow{event}X_{t+Delta t}
$$

This makes the application a dynamic operational simulator instead of a static notebook.

---

# 19. Live order stress stream

The default live stream produces:

$$
1\text{ order}/15\text{ seconds}
$$

which is:

$$
\frac{3600}{15}=240
$$

orders per real hour.

Configuration:

~~~text
LIVE_ORDER_STREAM=true
LIVE_ORDER_INTERVAL_S=15
~~~

This makes the system useful for repeated stress experiments involving:

- queue growth,
- consolidation,
- dispatching,
- optimizer latency,
- warehouse workload,
- fleet utilization,
- repositioning,
- disruptions.

---

# 20. Driver-in-the-loop decision layer

The system does not assume every optimization recommendation is automatically accepted.

A driver offer can include:

- vehicle ID,
- pickup warehouse,
- added kilometers,
- pickup ETA,
- estimated saving,
- SLA buffer,
- rejected alternatives,
- decision explanation.

The driver can accept or reject the proposed assignment.

Conceptually:

$$
\text{algorithmic recommendation}
\rightarrow
\text{human decision}
\rightarrow
\text{new system state}
$$

If rejected, the shipment is requeued and the engine can search for another assignment.

An automatic timeout is available for unattended demonstrations.

---

# 21. Explainable optimization

The system generates explanations such as:

> Capacity OK; road ETA 82 min; SLA buffer 115 min; detour 12.4%.

Candidate rejection reasons can include:

- capacity constraint,
- volume constraint,
- route-distance constraint,
- SLA violation,
- detour constraint,
- unavailable vehicle,
- routing feasibility failure.

This makes the optimization decision auditable.

The objective is not merely:

$$
\text{choose }v^*
$$

but:

$$
\text{choose }v^*
+
\text{show why competing candidates were infeasible}
$$

---

# 22. Traditional vs Adaptive evaluation

Every shipment has a counterfactual baseline.

## Traditional

Approximate:

$$
\text{shipment}
\rightarrow
\text{dedicated vehicle}
\rightarrow
\text{destination}
$$

Baseline attributes include:

- route distance,
- estimated travel time,
- cost,
- ETA.

## Adaptive

Adaptive attributes include:

- adaptive distance,
- adaptive time,
- allocated cost,
- cost saving,
- time saving,
- backhaul status.

This creates an explicit comparison:

$$
Delta C=C_{traditional}-C_{adaptive}
$$

$$
Delta T=T_{traditional}-T_{adaptive}
$$

The project can therefore evaluate the operational effect of consolidation instead of merely reporting that consolidation occurred.

---

# 23. Metrics exposed by the control tower

## Economics

- baseline assigned cost
- adaptive cost
- cost savings
- cost savings percentage

## Service

- average time saved
- on-time percentage
- at-risk shipments

## Consolidation

- consolidated shipments
- consolidation rate
- dispatches avoided

## Fleet

- fleet utilization
- current load
- remaining weight capacity
- remaining volume capacity

## Network efficiency

- total kilometers
- empty kilometers
- empty-kilometer percentage
- repositioning kilometers
- backhaul savings

## Optimization

- optimizer decision count
- optimizer p95 latency
- routing requests
- real routes
- fallback routes
- global optimization runs

---

# 24. Architecture

~~~text
                         Live Order Feed
                               |
                               v
                      Nearest Hub Mapping
                               |
                               v
                  +---------------------------+
                  | Online Route Insertion     |
                  |---------------------------|
                  | weight / volume capacity  |
                  | road ETA                  |
                  | pickup SLA                |
                  | delivery SLA              |
                  | detour                    |
                  | marginal cost             |
                  +-------------+-------------+
                                |
                    +-----------+-----------+
                    |                       |
                 feasible              infeasible
                    |                       |
                    v                       v
               Driver Offer         Warehouse Queue
                    |                       |
              accept/reject            full or 2h
                    |                       |
                    v                       v
               Route Update           Batch Dispatch
                    |                       |
                    +-----------+-----------+
                                |
                                v
                         OSRM Road Routing
                                |
                                v
                          Vehicle Simulator
                                |
                +---------------+---------------+
                |               |               |
                v               v               v
           Disruption       Forecast        Reposition
           Recovery             |             CP-SAT
                |               |               |
                +---------------+-------+-------+
                                        |
                                        v
                             Event / Replay / WS
                                        |
                                        v
                              Streamlit Control Tower
~~~

---

# 25. Repository structure

~~~text
adaptive-freight/
|
├── api/
|   └── main.py
|
├── src/
|   ├── engine.py
|   ├── optimizer.py
|   ├── global_optimizer.py
|   ├── forecast.py
|   ├── models.py
|   ├── geo.py
|   ├── router.py
|   ├── order_stream.py
|   ├── consolidation.py
|   ├── disruptions.py
|   ├── fleet.py
|   ├── simulator.py
|   ├── event_bus.py
|   ├── persistence.py
|   ├── auth.py
|   └── metrics.py
|
├── web/
|   ├── map.html
|   └── dashboard.html
|
├── tests/
|   ├── test_core.py
|   └── test_production.py
|
├── scripts/
|   ├── prepare_osrm.ps1
|   ├── start_private_osrm.ps1
|   ├── oci_bootstrap.sh
|   ├── oci_prepare_osrm.sh
|   └── oci_update.sh
|
├── app.py
├── Dockerfile
├── docker-compose.yml
├── docker-compose.oci.yml
├── requirements.txt
├── requirements-frontend.txt
├── render.yaml
└── OCI_DEPLOYMENT.md
~~~

---

# 26. Core modules

## src/models.py

Defines:

- Shipment
- Stop
- Vehicle

Shipment state contains:

- origin and destination,
- warehouse and handoff location,
- weight and volume,
- deadlines,
- service times,
- baseline metrics,
- adaptive metrics,
- queue timing,
- backhaul state,
- optimizer explanation.

Vehicle state contains:

- weight capacity,
- volume capacity,
- current and reserved load,
- current location,
- route stops,
- route geometry,
- total distance,
- empty distance,
- mission,
- reposition target.

## src/optimizer.py

Implements the online active-route insertion optimizer.

The decision pipeline is:

1. filter candidate vehicles,
2. check weight and volume,
3. enumerate pickup/delivery insertion positions,
4. obtain road travel-time hints,
5. include service times,
6. enforce time windows,
7. calculate detour,
8. calculate marginal cost,
9. score feasible candidates,
10. choose the highest-scoring feasible candidate.

## src/global_optimizer.py

Implements idle vehicle to warehouse repositioning with OR-Tools CP-SAT.

## src/forecast.py

Implements the rolling demand-rate estimator.

## src/engine.py

Coordinates the complete event-driven simulation and operational state.

---

# 27. API surface

The FastAPI backend exposes:

~~~text
GET  /health
GET  /version
GET  /ready
GET  /state
GET  /events
GET  /replay
GET  /shipments/{shipment_id}
GET  /driver-offers
GET  /metrics
GET  /city-coords
GET  /map

POST /orders
POST /driver-offers/{offer_id}
POST /fleet/reposition/{vehicle_id}/{warehouse}
POST /fleet/reoptimize
POST /control/start
POST /control/pause
POST /control/reset
POST /control/traffic/{factor}
POST /control/speed/{value}
POST /control/live-stream/{state}
POST /control/live-stream-interval/{seconds}
POST /disruptions/breakdown/{vehicle_id}
POST /disruptions/repair/{vehicle_id}

WebSocket:
    /ws
~~~

Example order:

~~~json
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
~~~

The same shipment ID can safely be retried because order ingestion is designed to be idempotent.

---

# 28. Replay and event history

Important operational events receive sequence numbers and simulation timestamps.

A typical shipment replay can look like:

~~~text
Order arrived
    |
    v
Nearest warehouse selected
    |
    v
Active vehicle candidates evaluated
    |
    v
Candidate rejected or selected
    |
    v
Driver notified
    |
    v
Route changed
    |
    v
Pickup
    |
    v
Delivery
~~~

A warehouse-consolidated shipment can follow:

~~~text
Order arrived
    |
    v
Warehouse queue
    |
    v
Queue accumulation
    |
    v
Departure threshold reached
    |
    v
Smallest suitable truck selected
    |
    v
Batch route created
    |
    v
Delivery
~~~

Replay is useful for validation, debugging, and explaining decisions.

---

# 29. Local development

Create a virtual environment:

~~~powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
~~~

Install:

~~~powershell
pip install -r requirements.txt
~~~

Run:

~~~powershell
python run.py
~~~

Open:

~~~text
http://127.0.0.1:8000/
~~~

The Streamlit frontend can be run separately using the frontend requirements.

---

# 30. Configuration

Important settings include:

~~~text
ROUTER_URL=https://router.project-osrm.org
PUBLIC_ROUTER_FALLBACK=true

LIVE_ORDER_STREAM=true
LIVE_ORDER_INTERVAL_S=15

SIM_MINUTES_PER_SECOND=2.0
ENGINE_TICK_MS=200
MAX_ORDERS_PER_TICK=50

DATABASE_URL=sqlite:///adaptive_freight.db
REDIS_URL=

API_KEYS=
CORS_ORIGINS=

PERSISTENCE_ENABLED=true
CHECKPOINT_INTERVAL_S=60
~~~

For public deployments, configure authentication.

Example:

~~~text
API_KEYS=sk_admin:admin,sk_dispatch:dispatcher,sk_view:readonly
~~~

Application roles:

- readonly
- dispatcher
- admin

---

# 31. Docker and deployment

The repository includes:

- Dockerfile
- Docker Compose
- OCI-specific Docker Compose
- Caddy HTTPS configuration
- Render deployment configuration
- OCI deployment scripts

Standard local stack:

~~~bash
docker compose up --build
~~~

The OCI stack combines:

~~~text
FastAPI
PostgreSQL
Redis
Private OSRM
Caddy HTTPS
~~~

with persistent Docker volumes and automatic container restart policies.

Detailed OCI instructions are in:

[OCI_DEPLOYMENT.md](OCI_DEPLOYMENT.md)

---

# 32. Render deployment

Current public backend:

**https://adaptive-freight.onrender.com**

The repository includes a GitHub Actions workflow:

~~~text
.github/workflows/render-keep-alive.yml
~~~

which periodically requests:

~~~text
GET /health
~~~

to reduce idle spin-down on the Render Free service.

The keep-alive mechanism does **not** remove the monthly Render Free instance-hour limit.

For the Render Free filesystem, persistence is intentionally disabled:

~~~text
PERSISTENCE_ENABLED=false
CHECKPOINT_INTERVAL_S=0
~~~

The project also includes persistent OCI deployment scaffolding for a VM-based deployment.

---

# 33. Testing and CI

The project contains automated tests for:

- active vehicle capacity,
- route feasibility,
- OSRM time feasibility,
- demand forecasting,
- authentication parsing,
- persistence,
- checkpoint round trips.

Run:

~~~bash
pytest -q
~~~

GitHub Actions is used for continuous integration.

---

# 34. Engineering and mathematical trade-offs

A full exact dynamic vehicle-routing model can become expensive as the network grows.

The current architecture therefore avoids a monolithic optimization call after every shipment.

Instead:

### Online layer

Use fast candidate generation and constraint filtering for active-route insertion.

### Global layer

Use CP-SAT periodically for idle-fleet repositioning.

This leads to:

$$
\text{fast online heuristic}
+
\text{small global combinatorial optimizer}
$$

The goal is not to claim global mathematical optimality at every simulation instant.

The goal is to obtain **interpretable, feasible, economically meaningful decisions at operational timescales**.

---

# 35. Why this is an Applied Mathematics project

The project was designed from a mathematical modeling perspective.

Its fundamental questions are expressed through:

- objective functions,
- feasible sets,
- inequalities,
- marginal quantities,
- time-window constraints,
- queue states,
- rate estimation,
- combinatorial assignments,
- counterfactual baselines,
- dynamic state transitions.

Instead of:

> "Can this truck take another order?"

the system asks:

$$
\text{Does there exist a feasible route }R'
$$

such that:

$$
Q_{weight}(R')\le Q_v
$$

$$
Q_{volume}(R')\le U_v
$$

$$
D(R')\le L_v
$$

$$
T_{pickup}(R')\le t_s^p
$$

$$
T_{delivery}(R')\le t_s^d
$$

and:

$$
C_{incremental}<C_{baseline}
$$

when the economic condition is required.

That translation from an operational question into a constrained mathematical decision is the main design philosophy.

---

# 36. Model assumptions

The project intentionally simplifies some real-world components.

### Travel

OSRM provides road-network routing, but live commercial fleet telemetry is not integrated.

### Demand

The current forecast is a rolling demand-rate estimator, not a trained ML forecasting model.

### Cost

The main transportation cost model is based on vehicle cost per kilometer.

A production system would additionally consider:

- fuel,
- tolls,
- labor,
- maintenance,
- accessorial charges,
- negotiated carrier rates.

### Loading

Weight and volume are modeled, but exact three-dimensional packing is not.

### Driver behavior

Accept/reject is modeled explicitly, but acceptance behavior is not statistically learned.

### Routing

The active-route decision combines OSRM travel-time information with route-distance heuristics rather than solving an exact road-network VRP for every event.

These assumptions are deliberate so the model remains interpretable and computationally usable.

---

# 37. Future mathematical extensions

## Stochastic optimization

Represent future demand and travel time as random variables:

$$
D\sim P_D
$$

$$
T\sim P_T
$$

and optimize expected cost plus delay risk:

$$
\min E[C]+\lambda P(\text{late})
$$

## Robust optimization

Let travel time belong to an uncertainty set:

$$
T\in\mathcal U
$$

and find a decision that remains feasible across the uncertainty set.

## Model predictive control

Repeat optimization over a moving horizon:

$$
t,\ t+Delta t,\ t+2Delta t,\ldots
$$

and execute only the first decision before re-optimizing.

## Learning-augmented optimization

Predict:

- demand,
- driver acceptance,
- travel-time residuals,
- SLA failure risk,

then feed those predictions into the constrained optimization model.

## Dynamic pickup-and-delivery VRP

Extend the current heuristic into a formal dynamic pickup-and-delivery VRP with:

- multiple depots,
- paired pickup and delivery,
- time windows,
- vehicle capacities,
- stochastic arrivals,
- exact route variables.

---

# 38. Production gap

This repository is a mathematically grounded optimization prototype, not a commercial transportation management system.

A true production TMS would need:

- GPS / ELD telemetry,
- WMS/TMS/ERP integration,
- carrier contracts and rates,
- driver mobile workflows,
- electronic proof of delivery,
- live traffic feeds,
- fuel and toll modeling,
- regulatory constraints,
- advanced loading constraints,
- distributed event storage,
- failure-tolerant messaging,
- production identity management,
- audit trails,
- monitoring and alerting,
- security hardening,
- routing-provider SLAs.

The project intentionally focuses on the mathematical and engineering core:

$$
\text{state}
\rightarrow
\text{constraints}
\rightarrow
\text{optimization}
\rightarrow
\text{decision}
\rightarrow
\text{new state}
$$

---

# 39. Project philosophy

The guiding principle is:

> **A logistics optimizer should not merely find a route. It should justify why the route is feasible, quantify what it saves, identify which constraints rejected alternatives, and continuously re-evaluate the network as conditions change.**

That leads to a platform in which optimization, simulation, and software engineering are connected.

The complete pipeline is:

~~~text
Mathematical model
        |
        v
Feasibility constraints
        |
        v
Optimization decision
        |
        v
Simulation state transition
        |
        v
API / WebSocket
        |
        v
Operational control tower
        |
        v
Metrics + counterfactual analysis
~~~

---

# 40. Key takeaway

Adaptive Freight is fundamentally a study of:

$$
\boxed{
\text{How should limited transportation capacity be allocated
over time and space under uncertainty and service constraints?}
}
$$

The answer is not a single shortest path.

It requires reasoning across:

- vehicle capacity,
- package volume,
- road travel time,
- time windows,
- warehouse queues,
- marginal route cost,
- empty movement,
- backhaul opportunities,
- disruption recovery,
- demand forecasting,
- fleet repositioning,
- driver decisions.

That is what makes Adaptive Freight an **Operations Research + Applied Mathematics + Data Science** project rather than a simple logistics dashboard.

---

## Author

**Harshith Devaraja**

M.Sc. Applied Mathematics and Computing

Focus areas:

- Operations Research
- Mathematical Optimization
- Data Science
- Machine Learning
- Simulation
- Risk and Decision Analytics
- Real-Time Optimization

GitHub:

https://github.com/Harshithpatali
