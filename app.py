from __future__ import annotations
import os, time, requests, streamlit as st
st.set_page_config(page_title="Adaptive Freight Control Tower", page_icon="🚚", layout="wide")
BACKEND=os.getenv("BACKEND_URL","http://127.0.0.1:8000")
API_KEY=os.getenv("DASHBOARD_API_KEY","")

def api(path, method="get"):
    try:
        headers={"X-API-Key":API_KEY} if API_KEY else {}
        r=getattr(requests,method)(BACKEND+path,timeout=2.0,headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error":str(e)}

st.title("🚚 Adaptive Freight — Real-Time Control Tower")
st.caption("Event-driven fleet optimization • live road geometry • dynamic consolidation • disruptions")
state=api("/state")
if "error" in state:
    st.error("Realtime engine is not running")
    st.code("python run.py")
    st.write(state["error"])
    st.stop()

with st.sidebar:
    st.header("Engine")
    c1,c2=st.columns(2)
    if c1.button("▶ LIVE",use_container_width=True): api("/control/start","post")
    if c2.button("⏸ PAUSE",use_container_width=True): api("/control/pause","post")
    if st.button("↻ RESET",use_container_width=True): api("/control/reset","post")
    speed=st.select_slider("Simulation speed (simulated min/sec)",[0.5,1,2,5,10,20,50,100],value=float(state.get("speed",2)))
    api(f"/control/speed/{speed}","post")
    st.write(f"Simulation: **{state['sim_time'][11:19]}**")
    st.write(f"Engine running: **{state['running']}**")
    st.write(f"Router: **{state['router']['url']}**")
    st.write(f"Road route quality: **{state['router']['real_ratio']}% real**")

st.subheader("Live Road Map")
st.info("Routes marked REAL come from OSRM road geometry. Fallback routes are explicitly marked.")
st.markdown(f'<iframe src="{BACKEND}/map" style="width:100%;height:680px;border:0;border-radius:12px"></iframe>', unsafe_allow_html=True)

m=state["metrics"]
cols=st.columns(7)
for c,label,val in zip(cols,["Processed","Consolidated","Dispatches","Queued","At risk","Utilization","Optimizer p95"],[state["orders_processed"],m["consolidated"],m["dispatched"],state["queue_depth"],m.get("at_risk",0),f"{m["fleet_utilization"]}%",f"{m["optimizer_p95_ms"]} ms"]):
    c.metric(label,val)

st.subheader("Event Stream")
for e in reversed(state["history"][-25:]):
    st.markdown(f"**{e.get('type','event').upper()}** · {e.get('message','')}")

time.sleep(1)
st.rerun()
