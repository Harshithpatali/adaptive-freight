from __future__ import annotations
import asyncio,time,statistics
from dataclasses import asdict
from datetime import datetime,timedelta
import pandas as pd
from .config import settings
from .geo import coords, nearest_city
from .models import Shipment, Vehicle, Stop, vehicle_from_dict
from .optimizer import Optimizer
from .event_bus import EventBus
from .router import RoadRouter
from . import persistence

class Engine:
    def __init__(self):
        self.cities=pd.read_csv(settings.base_dir / "data/cities.csv")
        self.vehicles_df=pd.read_csv(settings.base_dir / "data/vehicles.csv")
        if settings.demo_order_stream:
            order_path=settings.base_dir / "data/order_stream.csv"
            if not order_path.exists():
                from .order_stream import save_order_stream
                save_order_stream(5000,str(order_path))
            self.orders=pd.read_csv(order_path,parse_dates=["arrival_time","pickup_deadline","delivery_deadline"])
        else:
            self.orders=pd.DataFrame(columns=["shipment_id","arrival_time","pickup_city","delivery_city","weight_kg","volume_m3","revenue_usd","pickup_deadline","delivery_deadline","priority"])
        self.router=RoadRouter(); self.bus=EventBus(settings.redis_url); self.optimizer=Optimizer(self.router)
        self.lock=asyncio.Lock(); self.task=None; self.running=False; self.speed=settings.sim_minutes_per_second
        self.idx=0; self.sim_time=self.orders.iloc[0].arrival_time.to_pydatetime() if len(self.orders) else datetime.now()
        self.shipments={}; self.vehicles={}; self.history=[]; self.started_at=time.time(); self.route_tasks=set(); self.route_tasks_by_vehicle={}; self.live_queue=asyncio.Queue(); self.traffic_factor=settings.traffic_factor
        self.metrics={"processed":0,"consolidated":0,"dispatched":0,"queued":0,"delivered":0,"breakdowns":0,"km":0.0,"cost":0.0,"revenue":0.0}
        self._build_fleet(); self._last_checkpoint_at=0.0; self._checkpoint_task=None
        self._live_stream_enabled=settings.live_order_stream; self._live_stream_interval=settings.live_order_interval_s; self._live_stream_task=None; self._live_sampler=None

    def _build_fleet(self):
        speeds={"Cargo Van":58,"Box Truck":62,"Medium Truck":65,"Semi":68}
        for _,r in self.vehicles_df.iterrows():
            lat,lon=coords(r.base_city)
            v=Vehicle(r.vehicle_id,r.vehicle_type,float(r.capacity_kg),float(r.max_distance_km),float(r.cost_per_km),speeds.get(r.vehicle_type,62),r.base_city,lat,lon,r.base_city)
            self.vehicles[v.vehicle_id]=v

    async def restore(self):
        try: data=await asyncio.to_thread(persistence.load_latest_checkpoint)
        except Exception as e:
            await self.bus.publish({"type":"error","message":f"checkpoint load failed: {e}"}); return
        if not data:return
        self.idx=data.get("idx",self.idx); self.sim_time=datetime.fromisoformat(data["sim_time"]) if data.get("sim_time") else self.sim_time
        self.traffic_factor=data.get("traffic_factor",self.traffic_factor); self.metrics={**self.metrics,**data.get("metrics",{})}
        self.shipments={sid:Shipment.from_dict(sd) for sid,sd in data.get("shipments",{}).items()}
        if data.get("vehicles"): self.vehicles={vid:vehicle_from_dict(vd) for vid,vd in data["vehicles"].items()}
        self.history=data.get("history",self.history)
        await self.bus.publish({"type":"engine","action":"restored","message":f"Restored {len(self.shipments)} shipments and {len(self.vehicles)} vehicles from checkpoint"})

    def _serialize_state(self):
        return {"idx":self.idx,"sim_time":self.sim_time.isoformat(),"traffic_factor":self.traffic_factor,"metrics":self.metrics,
                "shipments":{sid:s.to_dict() for sid,s in self.shipments.items()},"vehicles":{vid:asdict(v) for vid,v in self.vehicles.items()},"history":self.history[-200:]}

    async def _maybe_checkpoint(self):
        now=time.time()
        if now-self._last_checkpoint_at<settings.checkpoint_interval_s:return
        if self._checkpoint_task and not self._checkpoint_task.done():return
        self._last_checkpoint_at=now; state=self._serialize_state()
        async def save():
            try: await asyncio.to_thread(persistence.save_checkpoint,state)
            except Exception as e: await self.bus.publish({"type":"error","message":f"checkpoint save failed: {e}"})
        self._checkpoint_task=asyncio.create_task(save())

    async def start(self):
        await self.router.start(); await self.bus.start()
        if not self.task or self.task.done():
            self.running=True; self.task=asyncio.create_task(self._loop()); await self.bus.publish({"type":"engine","action":"started"})
        if self._live_stream_enabled and (not self._live_stream_task or self._live_stream_task.done()):
            self._live_stream_task=asyncio.create_task(self._live_stream_loop())

    async def _live_stream_loop(self):
        if self._live_sampler is None:
            from .order_stream import make_live_order_sampler
            self._live_sampler=make_live_order_sampler()
        while self._live_stream_enabled and self.running:
            await asyncio.sleep(self._live_stream_interval)
            if not self._live_stream_enabled or not self.running: break
            await self.submit_live_order(self._live_sampler())

    async def set_live_stream(self,enabled:bool,interval_s:float|None=None):
        if interval_s is not None:self._live_stream_interval=interval_s
        self._live_stream_enabled=enabled
        if enabled and (not self._live_stream_task or self._live_stream_task.done()): self._live_stream_task=asyncio.create_task(self._live_stream_loop())
        await self.bus.publish({"type":"engine","action":"live_stream","enabled":enabled,"interval_s":self._live_stream_interval,
                                "message":f"Live order stream {'enabled — one order every '+str(int(self._live_stream_interval))+'s' if enabled else 'disabled'}"})

    async def stop(self):
        self.running=False
        if self.task:
            try: await asyncio.wait_for(self.task,timeout=2)
            except asyncio.TimeoutError: self.task.cancel()
        if self._live_stream_task and not self._live_stream_task.done(): self._live_stream_task.cancel()
        try: await asyncio.to_thread(persistence.save_checkpoint,self._serialize_state())
        except Exception: pass
        await self.router.close(); await self.bus.close()

    async def reset(self):
        async with self.lock:
            self.idx=0; self.sim_time=self.orders.iloc[0].arrival_time.to_pydatetime() if len(self.orders) else datetime.now()
            self.shipments={}; self.history=[]; self.metrics={"processed":0,"consolidated":0,"dispatched":0,"queued":0,"delivered":0,"breakdowns":0,"km":0.0,"cost":0.0,"revenue":0.0}; self.vehicles={}; self._build_fleet()
        await self.bus.publish({"type":"engine","action":"reset"})

    async def _loop(self):
        last=time.perf_counter()
        while self.running:
            now=time.perf_counter(); wall_dt=min(0.5,now-last); last=now
            try:
                await self.tick(wall_dt); await self._maybe_checkpoint()
            except Exception as e: await self.bus.publish({"type":"error","message":str(e)})
            await asyncio.sleep(settings.engine_tick_ms/1000)

    async def tick(self,wall_dt):
        sim_dt=wall_dt*self.speed*60; self.sim_time+=timedelta(seconds=sim_dt); await self._advance_vehicles(sim_dt); count=0
        while not self.live_queue.empty() and count<settings.max_orders_per_tick:
            o=await self.live_queue.get(); o.setdefault("arrival_time",self.sim_time); await self._process_order(o); count+=1
        while self.idx<len(self.orders) and pd.Timestamp(self.orders.iloc[self.idx].arrival_time).to_pydatetime()<=self.sim_time and count<settings.max_orders_per_tick:
            o=self.orders.iloc[self.idx].to_dict(); self.idx+=1; await self._process_order(o); count+=1
        await self._retry_queued(limit=min(10,settings.max_orders_per_tick-count if count<settings.max_orders_per_tick else 0))

    async def _process_order(self,order):
        if order["shipment_id"] in self.shipments:return
        shipment=Shipment(order["shipment_id"],order["pickup_city"],order["delivery_city"],float(order["weight_kg"]),float(order["volume_m3"]),float(order["revenue_usd"]),
                          pd.Timestamp(order["pickup_deadline"]).to_pydatetime(),pd.Timestamp(order["delivery_deadline"]).to_pydatetime(),order["priority"])
        self.shipments[shipment.shipment_id]=shipment
        decision,stats=self.optimizer.evaluate(list(self.vehicles.values()),shipment)
        if decision:
            v=decision["vehicle"]; was_active=bool(v.assigned_shipments) or v.status=="enroute"
            shipment.status="assigned"; shipment.assigned_vehicle=v.vehicle_id; shipment.assigned_at=self.sim_time; v.assigned_shipments.append(shipment.shipment_id)
            v.reserved_load_kg+=shipment.weight_kg; v.status="enroute"; v.stops=decision["stops"]
            self.metrics["consolidated"]+=1 if was_active else 0; self.metrics["dispatched"]+=0 if was_active else 1; self.metrics["revenue"]+=shipment.revenue_usd; self.metrics["processed"]+=1
            msg=f"CONSOLIDATE {shipment.shipment_id} → {v.vehicle_id}: +{decision['extra_km']:.1f} km, detour {decision['detour_pct']:.1f}%"
            event={"type":"order","shipment_id":shipment.shipment_id,"decision":"consolidate" if was_active else "dispatch","vehicle_id":v.vehicle_id,"message":msg if was_active else msg.replace("CONSOLIDATE","DISPATCH"),"optimizer_ms":stats["decision_ms"]}
            self.history.append(event); self.history=self.history[-200:]; await self.bus.publish(event); self._schedule_route(v)
        else:
            v=self._dispatch_new(shipment); self.metrics["processed"]+=1; self.metrics["revenue"]+=shipment.revenue_usd
            if v:
                shipment.status="assigned"; shipment.assigned_vehicle=v.vehicle_id; v.assigned_shipments.append(shipment.shipment_id); v.reserved_load_kg+=shipment.weight_kg
                v.status="enroute"; v.stops=[Stop(shipment.pickup_city,"pickup",[shipment.shipment_id]),Stop(shipment.delivery_city,"delivery",[shipment.shipment_id])]; self.metrics["dispatched"]+=1
                event={"type":"order","shipment_id":shipment.shipment_id,"decision":"dispatch","vehicle_id":v.vehicle_id,"message":f"DISPATCH {shipment.shipment_id} → {v.vehicle_id} ({v.vehicle_type})","optimizer_ms":stats["decision_ms"]}
            else:
                shipment.status="queued"; self.metrics["queued"]+=1
                event={"type":"order","shipment_id":shipment.shipment_id,"decision":"queued","vehicle_id":None,"message":f"QUEUE {shipment.shipment_id}: no feasible truck","optimizer_ms":stats["decision_ms"]}
            self.history.append(event); self.history=self.history[-200:]; await self.bus.publish(event)
            if v:self._schedule_route(v)

    async def _retry_queued(self,limit=10):
        if limit<=0:return
        for sh in [s for s in self.shipments.values() if s.status=="queued"][:limit]:
            decision,stats=self.optimizer.evaluate(list(self.vehicles.values()),sh)
            if not decision:continue
            v=decision["vehicle"]; was_active=bool(v.assigned_shipments) or v.status=="enroute"
            sh.status="assigned"; sh.assigned_vehicle=v.vehicle_id; sh.assigned_at=self.sim_time; v.assigned_shipments.append(sh.shipment_id)
            v.reserved_load_kg+=sh.weight_kg; v.status="enroute"; v.stops=decision["stops"]
            self.metrics["queued"]=max(0,self.metrics["queued"]-1); self.metrics["dispatched"]+=0 if was_active else 1; self.metrics["consolidated"]+=1 if was_active else 0
            e={"type":"reassignment","shipment_id":sh.shipment_id,"decision":"consolidate" if was_active else "dispatch","vehicle_id":v.vehicle_id,"message":f"REASSIGN {sh.shipment_id} → {v.vehicle_id}","optimizer_ms":stats["decision_ms"]}
            self.history.append(e); self.history=self.history[-200:]; await self.bus.publish(e); self._schedule_route(v)

    def _dispatch_new(self,shipment):
        idle=[v for v in self.vehicles.values() if v.status=="idle" and v.capacity_kg>=shipment.weight_kg]
        if not idle:return None
        idle.sort(key=lambda v:(v.capacity_kg,(coords(v.current_city)[0]-coords(shipment.pickup_city)[0])**2+(coords(v.current_city)[1]-coords(shipment.pickup_city)[1])**2))
        return idle[0]

    def _schedule_route(self,v):
        old=self.route_tasks_by_vehicle.get(v.vehicle_id)
        if old and not old.done(): old.cancel()
        v.route_version+=1; token=v.route_version
        async def task():
            points=[(v.current_lat,v.current_lon)]+[coords(s.city) for s in v.stops if not s.completed]
            t=time.perf_counter(); result=await self.router.route(points); ms=(time.perf_counter()-t)*1000
            async with self.lock:
                if token!=v.route_version or v.status=="broken":return
                v.geometry=result["geometry"]; v.route_total_km=float(result["distance_km"]); v.geometry_cum_km=[]; cum=0.0
                for i,p in enumerate(v.geometry):
                    if i:
                        from .geo import haversine_km
                        cum+=haversine_km(v.geometry[i-1],p)
                    v.geometry_cum_km.append(cum)
                v.progress_km=0.0; v.route_quality=result["quality"]; v.last_route_ms=ms
                legcum=0.0
                for idx,s in enumerate([s for s in v.stops if not s.completed]):
                    if idx<len(result.get("legs_km",[])):legcum+=result["legs_km"][idx]
                    s.cumulative_km=legcum
            await self.bus.publish({"type":"route","vehicle_id":v.vehicle_id,"quality":result["quality"],"route_ms":ms,"distance_km":result["distance_km"]})
        t=asyncio.create_task(task()); self.route_tasks.add(t); self.route_tasks_by_vehicle[v.vehicle_id]=t; t.add_done_callback(self.route_tasks.discard)

    async def _advance_vehicles(self,sim_seconds):
        for v in self.vehicles.values():
            if v.status!="enroute" or not v.geometry:continue
            old_progress=v.progress_km; step_km=v.speed_kmh*sim_seconds/3600*max(0.25,self.traffic_factor); v.progress_km=min(v.route_total_km,v.progress_km+step_km)
            traveled=max(0.0,v.progress_km-old_progress); v.total_distance_km+=traveled; self.metrics["km"]+=traveled; self.metrics["cost"]+=traveled*v.cost_per_km
            idx=0
            while idx<len(v.geometry_cum_km)-1 and v.geometry_cum_km[idx+1]<v.progress_km:idx+=1
            if idx<len(v.geometry)-1:
                a,b=v.geometry[idx],v.geometry[idx+1]; span=v.geometry_cum_km[idx+1]-v.geometry_cum_km[idx]; f=0 if span<=0 else (v.progress_km-v.geometry_cum_km[idx])/span
                v.current_lat=a[0]+f*(b[0]-a[0]); v.current_lon=a[1]+f*(b[1]-a[1]); v.current_city=nearest_city(v.current_lat,v.current_lon)
            elif v.geometry:v.current_lat,v.current_lon=v.geometry[-1]
            changed=False
            for s in v.stops:
                if s.completed or s.cumulative_km>v.progress_km+1e-6:continue
                s.completed=True; changed=True
                for sid in s.shipment_ids:
                    sh=self.shipments.get(sid)
                    if not sh:continue
                    if s.kind=="pickup" and sh.status=="assigned":
                        sh.status="picked_up"; v.reserved_load_kg=max(0,v.reserved_load_kg-sh.weight_kg); v.current_load_kg+=sh.weight_kg
                    elif s.kind=="delivery" and sh.status in ("picked_up","assigned"):
                        sh.status="delivered"; self.metrics["delivered"]+=1; v.current_load_kg=max(0,v.current_load_kg-sh.weight_kg); v.assigned_shipments=[x for x in v.assigned_shipments if x!=sid]
            if changed:
                v.stops=[s for s in v.stops if not s.completed]
                if not v.stops:
                    v.status="idle"; v.progress_km=0; v.geometry=[]; v.geometry_cum_km=[]; v.route_total_km=0; v.route_quality="none"

    async def submit_live_order(self,order:dict):
        await self.live_queue.put(order); await self.bus.publish({"type":"ingest","shipment_id":order.get("shipment_id"),"message":f"LIVE ORDER INGESTED {order.get('shipment_id')}"})
    async def set_traffic(self,factor:float):
        self.traffic_factor=factor; await self.bus.publish({"type":"network","traffic_factor":factor,"message":f"NETWORK TRAFFIC FACTOR {factor:.2f}x"})
    async def trigger_breakdown(self,vehicle_id:str):
        v=self.vehicles.get(vehicle_id)
        if not v:return False
        v.status="broken"; self.metrics["breakdowns"]+=1; affected=set(v.assigned_shipments)
        for sid in affected:
            sh=self.shipments.get(sid)
            if sh and sh.status!="delivered":sh.status="queued"; sh.assigned_vehicle=None
        v.assigned_shipments=[]; v.current_load_kg=0; v.reserved_load_kg=0; v.stops=[]; v.geometry=[]
        event={"type":"disruption","kind":"breakdown","vehicle_id":vehicle_id,"message":f"BREAKDOWN {vehicle_id}; {len(affected)} shipment(s) returned to queue"}
        self.history.append(event); self.history=self.history[-200:]; await self.bus.publish(event); return True
    async def recover_vehicle(self,vehicle_id):
        v=self.vehicles.get(vehicle_id)
        if not v:return False
        v.status="idle"; await self.bus.publish({"type":"disruption","kind":"repair","vehicle_id":vehicle_id,"message":f"REPAIRED {vehicle_id}"}); return True
    async def pause(self):self.running=False;await self.bus.publish({"type":"engine","action":"paused"})
    async def resume(self):await self.start();await self.bus.publish({"type":"engine","action":"resumed"})
    def snapshot(self):
        now=self.sim_time; at_risk=sum(1 for sh in self.shipments.values() if sh.status in ("assigned","picked_up") and sh.delivery_deadline<now+timedelta(minutes=30))
        return {"sim_time":self.sim_time.isoformat(),"running":self.running,"speed":self.speed,"orders_processed":self.metrics["processed"],"orders_total":len(self.orders),
                "queue_depth":sum(1 for s in self.shipments.values() if s.status=="queued"),
                "metrics":{**self.metrics,"consolidation_rate":round(100*self.metrics["consolidated"]/max(1,self.metrics["processed"]),1),
                           "fleet_utilization":round(sum(v.utilization_pct for v in self.vehicles.values())/max(1,len(self.vehicles)),1),"at_risk":at_risk,
                           "optimizer_p95_ms":self.optimizer.p95_ms(),"route_requests":self.router.requests,"real_routes":self.router.real_routes,"fallback_routes":self.router.fallback_routes},
                "vehicles":[v.snapshot() for v in self.vehicles.values()],"history":self.history[-50:],
                "router":{"url":self.router.base_url,"last_error":self.router.last_error,"real_ratio":round(100*self.router.real_routes/max(1,self.router.real_routes+self.router.fallback_routes),1),
                          "circuit_open":self.router.circuit_open,"retry_in_s":max(0,round(self.router.open_until-time.time(),1)) if self.router.circuit_open else 0},
                "live_stream":{"enabled":self._live_stream_enabled,"interval_s":self._live_stream_interval}}
