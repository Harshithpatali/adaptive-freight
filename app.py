from __future__ import annotations
import os
import json, time, requests, streamlit as st
from streamlit.components.v1 import html as st_html
st.set_page_config(page_title="Adaptive Freight Control Tower",page_icon="🚚",layout="wide")
BACKEND=os.getenv("BACKEND_URL","http://127.0.0.1:8000")
PUBLIC_BACKEND=os.getenv("PUBLIC_BACKEND_URL",BACKEND)
API_KEY=os.getenv("DASHBOARD_API_KEY","")

def api(path,method="get"):
    try:
        headers={"X-API-Key":API_KEY} if API_KEY else {}
        r=getattr(requests,method)(BACKEND+path,timeout=1.5,headers=headers); r.raise_for_status(); return r.json()
    except Exception as e:return {"error":str(e)}

st.title("🚚 Adaptive Freight — Real-Time Control Tower")
st.caption("Event-driven fleet optimization • live road geometry • dynamic consolidation • disruptions")
state=api('/state')
if 'error' in state:
    st.error("Realtime engine is not running")
    st.code("python run.py")
    st.write(state['error'])
    st.stop()

with st.sidebar:
    st.header("Engine")
    c1,c2=st.columns(2)
    if c1.button("▶ LIVE",use_container_width=True): api('/control/start','post')
    if c2.button("⏸ PAUSE",use_container_width=True): api('/control/pause','post')
    if st.button("↻ RESET",use_container_width=True): api('/control/reset','post')
    speed=st.select_slider("Simulation speed (simulated min/sec)",[0.5,1,2,5,10,20,50,100],value=float(state.get('speed',2)))
    api(f'/control/speed/{speed}','post')
    st.markdown("---")
    st.write(f"Simulation: **{state['sim_time'][11:19]}**")
    st.write(f"Engine running: **{state['running']}**")
    st.write(f"Router: **{state['router']['url']}**")
    st.write(f"Road route quality: **{state['router']['real_ratio']}% real**")
    st.markdown("---")
    st.subheader("Network")
    traffic=st.select_slider("Traffic factor",[0.6,0.8,1.0,1.25,1.5,2.0],value=1.0)
    if st.button("Apply traffic",use_container_width=True): api(f'/control/traffic/{traffic}','post')
    st.subheader("Disruption")
    vehicle_ids=[v['vehicle_id'] for v in state['vehicles']]
    chosen=st.selectbox("Vehicle",vehicle_ids)
    d1,d2=st.columns(2)
    if d1.button("Break",use_container_width=True): api(f'/disruptions/breakdown/{chosen}','post')
    if d2.button("Repair",use_container_width=True): api(f'/disruptions/repair/{chosen}','post')

m=state['metrics']; active=sum(v['status']=='enroute' for v in state['vehicles'])
cols=st.columns(7)
for c,label,val in zip(cols,["Processed","Consolidated","Dispatches","Queued","At risk","Utilization","Optimizer p95"],[state['orders_processed'],m['consolidated'],m['dispatched'],state['queue_depth'],m.get('at_risk',0),f"{m['fleet_utilization']}%",f"{m['optimizer_p95_ms']} ms"]): c.metric(label,val)

st.subheader("Live Road Map")
st.info("The map below is a live WebSocket client. Routes marked REAL come from OSRM road geometry. Fallback routes are straight-line estimates and are explicitly marked.")
html=f'<iframe src="{PUBLIC_BACKEND}/map" style="width:100%;height:680px;border:0;border-radius:12px"></iframe>'
st.markdown(html,unsafe_allow_html=True)

left,right=st.columns([1.35,1])
with left:
 st.subheader("Event Stream")
 for e in reversed(state['history'][-25:]): st.markdown(f"**{e.get('type','event').upper()}** · {e.get('message','')}")
with right:
 st.subheader("Fleet")
 rows=[]
 for v in state['vehicles']:
  rows.append({"Vehicle":v['vehicle_id'],"Status":v['status'],"City":v['current_city'],"Load":f"{v['load_kg']:.0f}/{v['capacity_kg']:.0f}","Util":f"{v['utilization_pct']:.0f}%","Route":v['route_quality']})
 st.dataframe(rows,use_container_width=True,height=420)

st.caption("Dashboard refreshes every second for KPIs; the map itself receives state over WebSocket for sub-second visual updates.")
time.sleep(1)
st.rerun()
