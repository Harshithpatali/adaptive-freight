from __future__ import annotations
import logging, time, uuid
from collections import defaultdict, deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Gauge, Histogram
from starlette.responses import Response
from pydantic import BaseModel, Field
import asyncio
from src.engine import Engine
from src.config import settings
from src import persistence
from src.auth import require_role, authorize_ws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log=logging.getLogger("adaptive_freight")
APP=FastAPI(title="Adaptive Freight Real-Time Optimization API",version="3.1")

if settings.cors_origins:
    APP.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
if not settings.auth_enabled:
    log.warning("API_KEYS is not set - every endpoint is UNAUTHENTICATED. Set API_KEYS before deploying this anywhere but localhost.")

_rate_buckets:dict[str,deque]=defaultdict(deque)

@APP.middleware("http")
async def rate_limit_and_log(request:Request,call_next):
    request_id=str(uuid.uuid4())[:8]; ident=request.headers.get("X-API-Key") or (request.client.host if request.client else "unknown")
    now=time.time(); bucket=_rate_buckets[ident]
    while bucket and now-bucket[0]>60:bucket.popleft()
    if len(bucket)>=settings.rate_limit_per_minute:return JSONResponse({"detail":"rate limit exceeded"},status_code=429)
    bucket.append(now); t0=time.perf_counter()
    try: response=await call_next(request)
    except Exception:
        log.exception("[%s] unhandled error on %s %s",request_id,request.method,request.url.path); raise
    response.headers["X-Request-ID"]=request_id
    log.info("[%s] %s %s -> %s (%.1fms)",request_id,request.method,request.url.path,response.status_code,(time.perf_counter()-t0)*1000)
    return response

ENGINE=Engine()
class LiveOrder(BaseModel):
    shipment_id:str
    pickup_city:str
    delivery_city:str
    weight_kg:float=Field(gt=0)
    volume_m3:float=1.0
    revenue_usd:float=500.0
    pickup_deadline:str|None=None
    delivery_deadline:str|None=None
    priority:str="Standard"

REQS=Counter("adaptive_requests_total","HTTP requests",["path"])
ORDERS=Counter("adaptive_orders_total","Orders processed")
OPT=Histogram("adaptive_optimizer_seconds","Optimizer latency")
ACTIVE=Gauge("adaptive_active_vehicles","Active vehicles")

@APP.on_event("startup")
async def startup():
    persistence.init_db(); await ENGINE.router.start(); await ENGINE.bus.start(); await ENGINE.restore(); await ENGINE.start()
@APP.on_event("shutdown")
async def shutdown(): await ENGINE.stop()

@APP.get("/health")
async def health(): return {"status":"ok","engine_task":bool(ENGINE.task and not ENGINE.task.done()),"running":ENGINE.running,"router":ENGINE.router.base_url}
@APP.get("/ready")
async def ready(): return {"ready":bool(ENGINE.task and not ENGINE.task.done()),"router_configured":bool(ENGINE.router.base_url)}
@APP.get("/state")
async def state(_role=Depends(require_role("readonly"))):
    s=ENGINE.snapshot(); ACTIVE.set(sum(v["status"]=="enroute" for v in s["vehicles"])); return s
@APP.get("/events")
async def events(_role=Depends(require_role("readonly"))): return ENGINE.history[-100:]
@APP.post("/control/start")
async def control_start(_role=Depends(require_role("admin"))): await ENGINE.resume(); return {"ok":True}
@APP.post("/control/pause")
async def control_pause(_role=Depends(require_role("admin"))): await ENGINE.pause(); return {"ok":True}
@APP.post("/control/reset")
async def control_reset(_role=Depends(require_role("admin"))): await ENGINE.reset(); return {"ok":True}
@APP.post("/orders")
async def ingest_order(order:LiveOrder,_role=Depends(require_role("dispatcher"))):
    from datetime import datetime,timedelta
    d=order.model_dump(); is_new=await asyncio.to_thread(persistence.record_order_if_new,order.shipment_id,d)
    if not is_new:return {"accepted":True,"duplicate":True,"shipment_id":order.shipment_id}
    now=ENGINE.sim_time; d["arrival_time"]=now
    d["pickup_deadline"]=datetime.fromisoformat(order.pickup_deadline) if order.pickup_deadline else now+timedelta(minutes=60)
    d["delivery_deadline"]=datetime.fromisoformat(order.delivery_deadline) if order.delivery_deadline else now+timedelta(minutes=240)
    await ENGINE.submit_live_order(d); ORDERS.inc()
    return {"accepted":True,"duplicate":False,"shipment_id":order.shipment_id}
@APP.post("/control/traffic/{factor}")
async def traffic(factor:float,_role=Depends(require_role("admin"))):
    if factor<=0 or factor>5:raise HTTPException(400,"traffic factor must be between 0 and 5")
    await ENGINE.set_traffic(factor); return {"traffic_factor":factor}
@APP.post("/control/speed/{value}")
async def control_speed(value:float,_role=Depends(require_role("admin"))):
    if value<=0 or value>600:raise HTTPException(400,"speed must be 0<value<=600 simulated minutes/sec")
    ENGINE.speed=value; return {"speed":ENGINE.speed}
@APP.post("/control/live-stream/{state}")
async def live_stream_toggle(state:str,_role=Depends(require_role("admin"))):
    if state not in ("enable","disable"):raise HTTPException(400,"state must be 'enable' or 'disable'")
    await ENGINE.set_live_stream(state=="enable"); return {"live_stream_enabled":ENGINE._live_stream_enabled,"interval_s":ENGINE._live_stream_interval}
@APP.post("/control/live-stream-interval/{seconds}")
async def live_stream_interval(seconds:float,_role=Depends(require_role("admin"))):
    if seconds<=0 or seconds>3600:raise HTTPException(400,"interval must be between 0 and 3600 seconds")
    await ENGINE.set_live_stream(ENGINE._live_stream_enabled,interval_s=seconds); return {"live_stream_enabled":ENGINE._live_stream_enabled,"interval_s":ENGINE._live_stream_interval}
@APP.post("/disruptions/breakdown/{vehicle_id}")
async def breakdown(vehicle_id:str,_role=Depends(require_role("dispatcher"))):return {"ok":await ENGINE.trigger_breakdown(vehicle_id)}
@APP.post("/disruptions/repair/{vehicle_id}")
async def repair(vehicle_id:str,_role=Depends(require_role("dispatcher"))):return {"ok":await ENGINE.recover_vehicle(vehicle_id)}
@APP.get("/metrics")
async def metrics():return Response(generate_latest(),media_type=CONTENT_TYPE_LATEST)
@APP.websocket("/ws")
async def ws(websocket:WebSocket):
    await websocket.accept()
    if not await authorize_ws(websocket):await websocket.close(code=4401);return
    async def event_loop():
        async for event in ENGINE.bus.subscribe():await websocket.send_json(event)
    async def state_loop():
        while True:
            await websocket.send_json({"type":"state","data":ENGINE.snapshot()}); await asyncio.sleep(settings.engine_tick_ms/1000)
    try:await asyncio.gather(event_loop(),state_loop())
    except (WebSocketDisconnect,asyncio.CancelledError):return
@APP.get("/city-coords")
async def city_coords():
    from src.geo import CITIES
    return {cid:[float(r.lat),float(r.lon)] for cid,r in CITIES.iterrows()}
@APP.get("/map",response_class=HTMLResponse)
async def map_page():return (settings.base_dir/"web"/"map.html").read_text(encoding="utf-8")
@APP.get("/",response_class=HTMLResponse)
async def dashboard_page():return (settings.base_dir/"web"/"dashboard.html").read_text(encoding="utf-8")
