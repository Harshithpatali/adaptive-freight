from __future__ import annotations

import os
import time
import requests
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Adaptive Freight | Data Science Control Tower",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stMetric"] { padding: 10px 12px; border: 1px solid rgba(128,128,128,.20); border-radius: 10px; }
[data-testid="stMetricValue"] { font-size: 1.45rem; }
.block-container { padding-top: 1.5rem; }
.ds-card { padding: 14px 16px; border: 1px solid rgba(128,128,128,.20); border-radius: 12px; }
.small-muted { color: #7f8a9a; font-size: .85rem; }
</style>
""", unsafe_allow_html=True)


def normalize_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value


BACKEND = normalize_url(os.getenv("BACKEND_URL", "http://127.0.0.1:8000"))
PUBLIC_BACKEND = normalize_url(os.getenv("PUBLIC_BACKEND_URL", BACKEND))
API_KEY = os.getenv("DASHBOARD_API_KEY", "")


def api(path: str, method: str = "get", payload: dict | None = None):
    try:
        headers = {"X-API-Key": API_KEY} if API_KEY else {}
        r = getattr(requests, method)(
            BACKEND + path,
            timeout=3.0,
            headers=headers,
            json=payload,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def fleet_frame(state: dict) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Vehicle": v["vehicle_id"],
            "Status": v["status"],
            "City": v["current_city"],
            "Load kg": round(v["load_kg"]),
            "Capacity kg": round(v["capacity_kg"]),
            "Utilization %": v["utilization_pct"],
            "Volume %": round(100 * v["volume_m3"] / max(v["volume_capacity_m3"], 1e-9), 1),
            "Route": v["route_quality"],
        }
        for v in state.get("vehicles", [])
    ])


def warehouse_frame(state: dict) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Warehouse": w["warehouse"],
            "Queued orders": w["queue_depth"],
            "Weight kg": round(w["weight_kg"]),
            "Volume m³": round(w["volume_m3"], 2),
            "Oldest wait min": round(w["oldest_wait_min"]),
            "Forecast orders / h": round(w["forecast_orders_next_hour"], 2),
            "Forecast weight kg / h": round(w["forecast_weight_kg_next_hour"]),
            "Idle trucks": w["available_vehicle_count"],
            "Departure due": w["departure_due"],
        }
        for w in state.get("warehouses", [])
    ])


def shipment_frame(state: dict) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Shipment": s["shipment_id"],
            "Traditional cost": s["traditional"]["cost_usd"],
            "Adaptive cost": s["adaptive"]["cost_usd"],
            "Cost saved": s["adaptive"]["cost_saving_usd"],
            "Traditional min": s["traditional"]["time_min"],
            "Adaptive min": s["adaptive"]["time_min"],
            "Time saved": s["adaptive"]["time_saving_min"],
            "Backhaul": s["adaptive"]["backhaul"],
        }
        for s in state.get("shipments", [])
    ])


st.title("📊 Adaptive Freight")
st.caption("Data Science Control Tower · optimization · forecasting · simulation · decision analytics")

state = api("/state")
if "error" in state:
    st.error("Realtime engine is not running")
    st.write(f"Backend: {BACKEND}")
    st.write(state["error"])
    st.stop()

m = state["metrics"]
fleet_df = fleet_frame(state)
warehouse_df = warehouse_frame(state)
ship_df = shipment_frame(state)

with st.sidebar:
    st.header("Experiment Controls")
    live_monitoring = st.toggle(
        "Enable live monitoring",
        value=False,
        help="Opens the live road-map WebSocket while this dashboard is active.",
    )
    if live_monitoring:
        api("/control/live-stream/enable", "post")
        st.success("Live monitoring enabled")
    else:
        api("/control/live-stream/disable", "post")
        st.info("Live monitoring disabled")

    st.divider()
    st.subheader("Simulation")
    c1, c2 = st.columns(2)
    if c1.button("▶ Run", use_container_width=True):
        api("/control/start", "post")
    if c2.button("⏸ Pause", use_container_width=True):
        api("/control/pause", "post")
    if st.button("↻ Reset experiment", use_container_width=True):
        api("/control/reset", "post")
        st.rerun()

    speed = st.select_slider(
        "Simulation speed",
        [0.5, 1, 2, 5, 10, 20, 50, 100],
        value=float(state.get("speed", 2)),
    )
    if st.button("Apply speed", use_container_width=True):
        api(f"/control/speed/{speed}", "post")

    st.divider()
    st.subheader("Scenario")
    traffic = st.select_slider(
        "Traffic multiplier",
        [0.6, 0.8, 1.0, 1.25, 1.5, 2.0],
        value=1.0,
    )
    if st.button("Apply traffic", use_container_width=True):
        api(f"/control/traffic/{traffic}", "post")

    vehicle_ids = [v["vehicle_id"] for v in state["vehicles"]]
    if vehicle_ids:
        chosen = st.selectbox("Vehicle disruption", vehicle_ids)
        c1, c2 = st.columns(2)
        if c1.button("Break", use_container_width=True):
            api(f"/disruptions/breakdown/{chosen}", "post")
        if c2.button("Repair", use_container_width=True):
            api(f"/disruptions/repair/{chosen}", "post")

    st.divider()
    st.caption(f"Simulation time: {state['sim_time'][11:19]}")
    st.caption(f"Engine running: {state['running']}")
    st.caption(f"Router: {state['router']['url']}")

# KPI layer: make the analytical story visible immediately.
st.subheader("Executive experiment summary")
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Orders processed", state["orders_processed"])
k2.metric("Consolidation", f"{m['consolidation_rate']:.1f}%")
k3.metric("Fleet utilization", f"{m['fleet_utilization']:.1f}%")
k4.metric("Cost reduction", f"{m['cost_savings_pct']:.1f}%")
k5.metric("On-time delivery", f"{m['on_time_pct']:.1f}%")
k6.metric("Optimizer p95", f"{m['optimizer_p95_ms']:.0f} ms")

st.caption(
    f"Adaptive allocation: USD {m['allocated_actual_cost']:,.0f} · "
    f"traditional baseline: USD {m['baseline_cost']:,.0f} · "
    f"dispatches avoided: {m['dispatches_avoided']}"
)

tab_overview, tab_analytics, tab_operations, tab_decisions = st.tabs(
    ["📈 Overview", "🧪 Data Science", "🚚 Operations", "🧠 Decisions"]
)

with tab_overview:
    left, right = st.columns([1.35, 1])
    with left:
        st.markdown("### Cost and service outcomes")
        outcome = pd.DataFrame(
            {
                "Strategy": ["Traditional", "Adaptive"],
                "Cost (USD)": [m["baseline_cost"], m["allocated_actual_cost"]],
                "Time (min)": [
                    sum(s["traditional"]["time_min"] for s in state.get("shipments", [])),
                    sum(s["adaptive"]["time_min"] for s in state.get("shipments", [])),
                ],
            }
        ).set_index("Strategy")
        st.bar_chart(outcome, y=["Cost (USD)", "Time (min)"], height=300)

    with right:
        st.markdown("### Decision-quality indicators")
        quality = pd.DataFrame(
            {
                "Metric": [
                    "Cost reduction %",
                    "On-time %",
                    "Consolidation %",
                    "Fleet utilization %",
                    "Real-route share %",
                ],
                "Value": [
                    m["cost_savings_pct"],
                    m["on_time_pct"],
                    m["consolidation_rate"],
                    m["fleet_utilization"],
                    100 * m["real_routes"] / max(m["real_routes"] + m["fallback_routes"], 1),
                ],
            }
        )
        st.dataframe(quality, hide_index=True, use_container_width=True)

    st.markdown("### Warehouse demand profile")
    if not warehouse_df.empty:
        chart = warehouse_df.set_index("Warehouse")[
            ["Queued orders", "Forecast orders / h", "Idle trucks"]
        ]
        st.bar_chart(chart, height=320)
    else:
        st.info("No warehouse observations yet.")

with tab_analytics:
    st.markdown("### Fleet utilization distribution")
    if not fleet_df.empty:
        c1, c2 = st.columns(2)
        with c1:
            st.bar_chart(
                fleet_df.set_index("Vehicle")[["Utilization %", "Volume %"]],
                height=300,
            )
        with c2:
            st.dataframe(
                fleet_df.sort_values("Utilization %", ascending=False),
                hide_index=True,
                use_container_width=True,
                height=300,
            )

    st.markdown("### Forecast vs current queue")
    if not warehouse_df.empty:
        forecast = warehouse_df.set_index("Warehouse")[
            ["Queued orders", "Forecast orders / h"]
        ]
        st.line_chart(forecast, height=300)

    st.markdown("### Event distribution")
    events = state.get("history", [])
    if events:
        event_counts = (
            pd.Series([e.get("type", "event") for e in events])
            .value_counts()
            .rename("Events")
            .to_frame()
        )
        st.bar_chart(event_counts, height=260)
    else:
        st.info("No events recorded.")

with tab_operations:
    st.markdown("### Warehouse queue state")
    if not warehouse_df.empty:
        st.dataframe(
            warehouse_df.sort_values(
                ["Departure due", "Oldest wait min"],
                ascending=[False, False],
            ),
            hide_index=True,
            use_container_width=True,
        )

    st.markdown("### Fleet state")
    st.dataframe(fleet_df, hide_index=True, use_container_width=True, height=390)

    st.markdown("### Live road map")
    st.info("Real OSRM routes are solid; fallback routes are explicitly marked.")
    if live_monitoring:
        st.markdown(
            f'<iframe src="{PUBLIC_BACKEND}/map" '
            'style="width:100%;height:680px;border:0;border-radius:12px"></iframe>',
            unsafe_allow_html=True,
        )
    else:
        st.warning("Enable live monitoring to open the real-time map.")

with tab_decisions:
    st.markdown("### Shipment-level counterfactual analysis")
    if not ship_df.empty:
        st.dataframe(
            ship_df.sort_values("Cost saved", ascending=False),
            hide_index=True,
            use_container_width=True,
            height=320,
        )
        st.markdown("**Adaptive savings distribution**")
        st.bar_chart(
            ship_df.set_index("Shipment")[["Traditional cost", "Adaptive cost"]],
            height=280,
        )
    else:
        st.info("Shipment comparison data will appear as the experiment runs.")

    st.markdown("### Demand forecast + fleet repositioning")
    if not warehouse_df.empty:
        st.dataframe(
            warehouse_df[
                [
                    "Warehouse",
                    "Forecast orders / h",
                    "Forecast weight kg / h",
                    "Idle trucks",
                    "Departure due",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )

    if st.button("Run global repositioning"):
        api("/fleet/reoptimize", "post")
        st.rerun()

    if state.get("reposition_plan"):
        st.markdown("**Recommended repositioning plan**")
        st.dataframe(pd.DataFrame(state["reposition_plan"]), hide_index=True, use_container_width=True)

    st.markdown("### Driver offers")
    offers = state.get("driver_offers", [])
    if not offers:
        st.success("No pending driver offers.")
    for offer in offers[:12]:
        c1, c2, c3 = st.columns([2.3, 2.0, 1])
        c1.write(f"**{offer['offer_id']} · {offer['vehicle_id']}**")
        c1.caption(f"{offer['shipment_id']} · {offer['warehouse']}")
        c2.write(
            f"+{offer['incremental_km']:.1f} km · "
            f"Saving USD {offer['savings_usd']:,.0f}"
        )
        c2.caption(f"SLA buffer: {offer['sla_buffer_min']:.0f} min")
        if c3.button("Accept", key=f"accept_{offer['offer_id']}", use_container_width=True):
            api(f"/driver-offers/{offer['offer_id']}", "post", {"accept": True})
            st.rerun()
        if c3.button("Reject", key=f"reject_{offer['offer_id']}", use_container_width=True):
            api(f"/driver-offers/{offer['offer_id']}", "post", {"accept": False})
            st.rerun()

    st.markdown("### Event replay")
    history = state.get("history", [])
    if history:
        replay_idx = st.slider("Replay event", 0, len(history) - 1, len(history) - 1)
        ev = history[replay_idx]
        st.code(
            f"[{ev.get('timestamp', '')}] #{ev.get('seq', '')} "
            f"{ev.get('type', 'event').upper()}\n{ev.get('message', '')}"
        )

st.markdown("---")
st.caption(
    "Research-style dashboard: simulation data → feature/forecast signals → optimization → "
    "counterfactual comparison → operational decision."
)

time.sleep(5)
st.rerun()
