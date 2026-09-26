from __future__ import annotations
import os,time,requests
import streamlit as st

st.set_page_config(page_title="Adaptive Freight Control Tower",page_icon="🚚",layout="wide")

def normalize_url(value:str)->str:
    value=(value or "").strip().rstrip("/")
    if value and not value.startswith(("http://","https://")):value="http://"+value
    return value

BACKEND=normalize_url(os.getenv("BACKEND_URL","http://127.0.0.1:8000"))
PUBLIC_BACKEND=normalize_url(os.getenv("PUBLIC_BACKEND_URL",BACKEND))
API_KEY=os.getenv("DASHBOARD_API_KEY","")

def api(path:str,method:str="get"):
    try:
        headers={"X-API-Key":API_KEY} if API_KEY else {}
        r=getattr(requests,method)(BACKEND+path,timeout=2.5,headers=headers)
        r.raise_for_status();return r.json()
    except Exception as exc:return {"error":str(exc)}

st.title("🚚 Adaptive Freight — Real-Time Control Tower")
st.caption("240 orders/hour stress stream • nearest-warehouse allocation • active-route consolidation • full-load / 2-hour departures")

state=api("/state")
if "error" in state:
    st.error("Realtime engine is not running");st.write(f"Backend: {BACKEND}");st.write(state["error"]);st.stop()

with st.sidebar:
    st.header("Engine")
    c1,c2=st.columns(2)
    if c1.button("▶ LIVE",use_container_width=True):api("/control/start","post")
    if c2.button("⏸ PAUSE",use_container_width=True):api("/control/pause","post")
    if st.button("↻ RESET",use_container_width=True):api("/control/reset","post")
    speed=st.select_slider("Simulation speed (simulated min/sec)",[0.5,1,2,5,10,20,50,100],value=float(state.get("speed",2)))
    api(f"/control/speed/{speed}","post")
    st.markdown("---")
    st.write(f"Simulation: **{state['sim_time'][11:19]}**")
    st.write(f"Engine running: **{state['running']}**")
    st.write(f"Router: **{state['router']['url']}**")
    st.write(f"Road route quality: **{state['router']['real_ratio']}% real**")
    st.markdown("---")
    st.subheader("Network")
    traffic=st.select_slider("Traffic factor",[0.6,0.8,1.0,1.25,1.5,2.0],value=1.0)
    if st.button("Apply traffic",use_container_width=True):api(f"/control/traffic/{traffic}","post")
    st.subheader("Disruption")
    vehicle_ids=[v["vehicle_id"] for v in state["vehicles"]]
    chosen=st.selectbox("Vehicle",vehicle_ids)
    d1,d2=st.columns(2)
    if d1.button("Break",use_container_width=True):api(f"/disruptions/breakdown/{chosen}","post")
    if d2.button("Repair",use_container_width=True):api(f"/disruptions/repair/{chosen}","post")
    st.subheader("Live Orders")
    st.write(f"**1 order / {state['live_stream']['interval_s']:.0f}s**")
    st.write("Warehouse departure rule: **full load or 2 simulated hours**")

m=state["metrics"]
cols=st.columns(9)
cards=[
    ("Orders",state["orders_processed"]),
    ("Consolidated",m["consolidated"]),
    ("Truck dispatches",m["dispatched"]),
    ("Dispatches avoided",m["dispatches_avoided"]),
    ("Warehouse queue",state["queue_depth"]),
    ("Utilization",f"{m['fleet_utilization']}%"),
    ("Cost saved",f"USD {m['cost_savings_usd']:,.0f}"),
    ("Time saved",f"{m['time_saved_min']:,.0f} min"),
    ("Optimizer p95",f"{m['optimizer_p95_ms']} ms"),
]
for col,(label,value) in zip(cols,cards):col.metric(label,value)

c1,c2,c3=st.columns(3)
c1.metric("Baseline dedicated-shipping cost",f"USD {m['baseline_cost']:,.0f}")
c2.metric("Allocated optimized cost",f"USD {m['allocated_actual_cost']:,.0f}")
c3.metric("Cost reduction",f"{m['cost_savings_pct']:.1f}%")

st.subheader("Warehouse Queues")
if state["warehouses"]:
    st.dataframe([
        {"Warehouse":w["warehouse"],"Queued orders":w["queue_depth"],"Weight kg":round(w["weight_kg"]),
         "Volume m³":round(w["volume_m3"],2),"Oldest wait":f"{w['oldest_wait_min']:.0f} min",
         "Departure":("DUE" if w["departure_due"] else "waiting")}
        for w in state["warehouses"]
    ],use_container_width=True,height=230)
else:
    st.info("No shipment is currently waiting at a warehouse.")

st.subheader("Live Road Map")
st.info("REAL routes come from OSRM road geometry. Fallback routes are explicitly marked.")
st.markdown(f'<iframe src="{PUBLIC_BACKEND}/map" style="width:100%;height:680px;border:0;border-radius:12px"></iframe>',unsafe_allow_html=True)

left,right=st.columns([1.35,1])
with left:
    st.subheader("Driver / Optimizer Event Stream")
    for event in reversed(state["history"][-25:]):
        st.markdown(f"**{event.get('type','event').upper()}** · {event.get('message','')}")
with right:
    st.subheader("Fleet")
    rows=[{"Vehicle":v["vehicle_id"],"Status":v["status"],"City":v["current_city"],
           "Load":f"{v['load_kg']:.0f}/{v['capacity_kg']:.0f} kg","Volume":f"{v['volume_m3']:.1f}/{v['volume_capacity_m3']:.1f} m³",
           "Util":f"{v['utilization_pct']:.0f}%","Route":v["route_quality"]} for v in state["vehicles"]]
    st.dataframe(rows,use_container_width=True,height=420)

st.caption("Each live order gets a package weight/size, nearest warehouse, baseline dedicated-truck estimate, and an optimized assignment decision.")
time.sleep(1)
st.rerun()
