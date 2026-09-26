from __future__ import annotations
import asyncio,time
from dataclasses import asdict
from datetime import datetime,timedelta
import pandas as pd
from .config import settings
from .geo import coords,nearest_city,nearest_warehouse,city_distance,route_distance
from .models import Shipment,Vehicle,Stop,vehicle_from_dict
from .optimizer import Optimizer
from .event_bus import EventBus
from .router import RoadRouter
from . import persistence
from .forecast import DemandForecaster
from .global_optimizer import GlobalFleetOptimizer

class Engine:
    WAREHOUSE_DEPARTURE_MIN=120.0
    FULL_LOAD_PCT=90.0

    def __init__(self):
        self.cities=pd.read_csv(settings.base_dir/"data/cities.csv")
        self.vehicles_df=pd.read_csv(settings.base_dir/"data/vehicles.csv")
        if settings.demo_order_stream:
            order_path=settings.base_dir/"data/order_stream.csv"
            if not order_path.exists():
                from .order_stream import save_order_stream
                save_order_stream(5000,str(order_path))
            self.orders=pd.read_csv(order_path,parse_dates=["arrival_time","pickup_deadline","delivery_deadline"])
        else:
            self.orders=pd.DataFrame(columns=["shipment_id","arrival_time","pickup_city","delivery_city","weight_kg","volume_m3","package_length_m","package_width_m","package_height_m","revenue_usd","pickup_deadline","delivery_deadline","priority"])
        self.router=RoadRouter();self.bus=EventBus(settings.redis_url);self.optimizer=Optimizer(self.router)
        self.lock=asyncio.Lock();self.task=None;self.running=False;self.speed=settings.sim_minutes_per_second
        self.idx=0;self.sim_time=self.orders.iloc[0].arrival_time.to_pydatetime() if len(self.orders) else datetime.now()
        self.shipments={};self.vehicles={};self.history=[];self.started_at=time.time()
        self.route_tasks=set();self.route_tasks_by_vehicle={};self.live_queue=asyncio.Queue();self.traffic_factor=settings.traffic_factor
        self.warehouse_queues={};self.warehouse_first_arrival={}
        self.forecaster=DemandForecaster()
        self.global_optimizer=GlobalFleetOptimizer()
        self.last_global_opt_sim=self.sim_time
        self.driver_offers={}
        self.offer_counter=0
        self.event_seq=0
        self.metrics={"processed":0,"consolidated":0,"warehouse_batched":0,"dispatched":0,"queued":0,"delivered":0,
                      "breakdowns":0,"km":0.0,"cost":0.0,"revenue":0.0,"baseline_cost":0.0,"allocated_actual_cost":0.0,
                      "time_saved_min":0.0,"warehouse_departures":0,"baseline_km":0.0,"baseline_time_min":0.0,"empty_km":0.0,"backhaul_shipments":0,"backhaul_km_saved":0.0,"driver_accepts":0,"driver_rejects":0,"breakdown_transfers":0,"reposition_km":0.0}
        self._build_fleet();self._last_checkpoint_at=0.0;self._checkpoint_task=None
        self._live_stream_enabled=settings.live_order_stream;self._live_stream_interval=settings.live_order_interval_s
        self._live_stream_task=None;self._live_sampler=None

    def _record(self,event):
        event=dict(event)
        self.event_seq+=1
        event.setdefault("seq",self.event_seq)
        event.setdefault("timestamp",self.sim_time.isoformat())
        self.history.append(event)
        self.history=self.history[-300:]
        return event

    async def _emit(self,event):
        event=self._record(event)
        await self.bus.publish(event)
        return event

    def _build_fleet(self):
        speeds={"Cargo Van":58,"Box Truck":62,"Medium Truck":65,"Semi":68}
        volume_caps={"Cargo Van":8.0,"Box Truck":28.0,"Medium Truck":48.0,"Semi":82.0}
        for _,r in self.vehicles_df.iterrows():
            lat,lon=coords(r.base_city)
            self.vehicles[r.vehicle_id]=Vehicle(
                vehicle_id=r.vehicle_id,vehicle_type=r.vehicle_type,capacity_kg=float(r.capacity_kg),
                max_distance_km=float(r.max_distance_km),cost_per_km=float(r.cost_per_km),
                speed_kmh=speeds.get(r.vehicle_type,62),base_city=r.base_city,
                current_lat=lat,current_lon=lon,current_city=r.base_city,
                volume_capacity_m3=volume_caps.get(r.vehicle_type,28.0))

    async def restore(self):
        try:data=await asyncio.to_thread(persistence.load_latest_checkpoint)
        except Exception as e:
            await self.bus.publish({"type":"error","message":f"checkpoint load failed: {e}"});return
        if not data:return
        self.idx=data.get("idx",self.idx);self.sim_time=datetime.fromisoformat(data["sim_time"]) if data.get("sim_time") else self.sim_time
        self.traffic_factor=data.get("traffic_factor",self.traffic_factor);self.metrics={**self.metrics,**data.get("metrics",{})}
        self.shipments={sid:Shipment.from_dict(sd) for sid,sd in data.get("shipments",{}).items()}
        if data.get("vehicles"):self.vehicles={vid:vehicle_from_dict(vd) for vid,vd in data["vehicles"].items()}
        self.history=data.get("history",self.history)
        for sh in self.shipments.values():
            if sh.status=="queued" and sh.warehouse_city:
                self.warehouse_queues.setdefault(sh.warehouse_city,[]).append(sh.shipment_id)
                self.warehouse_first_arrival.setdefault(sh.warehouse_city,sh.queued_since or self.sim_time)

    def _serialize_state(self):
        return {"idx":self.idx,"sim_time":self.sim_time.isoformat(),"traffic_factor":self.traffic_factor,"metrics":self.metrics,
                "shipments":{sid:s.to_dict() for sid,s in self.shipments.items()},"vehicles":{vid:asdict(v) for vid,v in self.vehicles.items()},
                "history":self.history[-200:]}

    async def _maybe_checkpoint(self):
        if settings.checkpoint_interval_s<=0:return
        now=time.time()
        if now-self._last_checkpoint_at<settings.checkpoint_interval_s:return
        if self._checkpoint_task and not self._checkpoint_task.done():return
        self._last_checkpoint_at=now;state=self._serialize_state()
        async def save():
            try:await asyncio.to_thread(persistence.save_checkpoint,state)
            except Exception as e:await self.bus.publish({"type":"error","message":f"checkpoint save failed: {e}"})
        self._checkpoint_task=asyncio.create_task(save())

    async def start(self):
        await self.router.start();await self.bus.start()
        if not self.task or self.task.done():
            self.running=True;self.task=asyncio.create_task(self._loop());await self.bus.publish({"type":"engine","action":"started"})
        if self._live_stream_enabled and (not self._live_stream_task or self._live_stream_task.done()):
            self._live_stream_task=asyncio.create_task(self._live_stream_loop())

    async def _live_stream_loop(self):
        if self._live_sampler is None:
            from .order_stream import make_live_order_sampler
            self._live_sampler=make_live_order_sampler()
        while self._live_stream_enabled and self.running:
            await asyncio.sleep(self._live_stream_interval)
            if not self._live_stream_enabled or not self.running:break
            await self.submit_live_order(self._live_sampler())

    async def set_live_stream(self,enabled:bool,interval_s:float|None=None):
        if interval_s is not None:self._live_stream_interval=interval_s
        self._live_stream_enabled=enabled
        if enabled and (not self._live_stream_task or self._live_stream_task.done()):
            self._live_stream_task=asyncio.create_task(self._live_stream_loop())
        await self.bus.publish({"type":"engine","action":"live_stream","enabled":enabled,"interval_s":self._live_stream_interval,
                                "message":f"Live order stream {'enabled — one order every '+str(int(self._live_stream_interval))+'s' if enabled else 'disabled'}"})

    async def stop(self):
        self.running=False
        if self.task:
            try:await asyncio.wait_for(self.task,timeout=2)
            except asyncio.TimeoutError:self.task.cancel()
        if self._live_stream_task and not self._live_stream_task.done():self._live_stream_task.cancel()
        try:await asyncio.to_thread(persistence.save_checkpoint,self._serialize_state())
        except Exception:pass
        await self.router.close();await self.bus.close()

    async def reset(self):
        async with self.lock:
            self.idx=0;self.sim_time=self.orders.iloc[0].arrival_time.to_pydatetime() if len(self.orders) else datetime.now()
            self.shipments={};self.history=[];self.warehouse_queues={};self.warehouse_first_arrival={}
            self.metrics={"processed":0,"consolidated":0,"warehouse_batched":0,"dispatched":0,"queued":0,"delivered":0,
                          "breakdowns":0,"km":0.0,"cost":0.0,"revenue":0.0,"baseline_cost":0.0,"allocated_actual_cost":0.0,
                          "time_saved_min":0.0,"warehouse_departures":0}
            self.vehicles={};self._build_fleet()
        await self.bus.publish({"type":"engine","action":"reset"})

    async def _loop(self):
        last=time.perf_counter()
        while self.running:
            now=time.perf_counter();wall_dt=min(0.5,now-last);last=now
            try:await self.tick(wall_dt);await self._maybe_checkpoint()
            except Exception as e:await self.bus.publish({"type":"error","message":str(e)})
            await asyncio.sleep(settings.engine_tick_ms/1000)

    async def tick(self,wall_dt):
        sim_dt=wall_dt*self.speed*60;self.sim_time+=timedelta(seconds=sim_dt);await self._advance_vehicles(sim_dt)
        count=0
        while not self.live_queue.empty() and count<settings.max_orders_per_tick:
            o=await self.live_queue.get();o.setdefault("arrival_time",self.sim_time);await self._process_order(o);count+=1
        while self.idx<len(self.orders) and pd.Timestamp(self.orders.iloc[self.idx].arrival_time).to_pydatetime()<=self.sim_time and count<settings.max_orders_per_tick:
            o=self.orders.iloc[self.idx].to_dict();self.idx+=1;await self._process_order(o);count+=1
        await self._retry_queued(limit=min(10,max(0,settings.max_orders_per_tick-count)));await self._dispatch_due_warehouses()

    def _baseline_plan(self,shipment):
        warehouse=shipment.warehouse_city or shipment.pickup_city
        candidates=[v for v in self.vehicles.values() if v.capacity_kg>=shipment.weight_kg and v.volume_capacity_m3>=shipment.volume_m3]
        if not candidates:candidates=[v for v in self.vehicles.values() if v.capacity_kg>=shipment.weight_kg]
        if not candidates:candidates=list(self.vehicles.values())
        if not candidates:return
        best=min(candidates,key=lambda v:(v.cost_per_km,v.capacity_kg));dist=city_distance(warehouse,shipment.delivery_city)
        shipment.baseline_cost_usd=round(dist*best.cost_per_km,2);shipment.baseline_time_min=round(dist/max(1.0,best.speed_kmh)*60+15,2)
        shipment.baseline_eta=self.sim_time+timedelta(minutes=shipment.baseline_time_min);self.metrics["baseline_cost"]+=shipment.baseline_cost_usd

    def _queue_order(self,shipment):
        wh=shipment.warehouse_city
        if not wh:return
        self.warehouse_queues.setdefault(wh,[]).append(shipment.shipment_id);shipment.status="queued";shipment.queued_since=shipment.queued_since or self.sim_time
        self.warehouse_first_arrival.setdefault(wh,shipment.queued_since);self.metrics["queued"]+=1

    def _remove_from_warehouse_queue(self,shipment_id,warehouse):
        if not warehouse:return
        q=self.warehouse_queues.get(warehouse,[])
        if shipment_id in q:q.remove(shipment_id)
        if not q:self.warehouse_queues.pop(warehouse,None);self.warehouse_first_arrival.pop(warehouse,None)
        else:
            vals=[self.shipments[sid].queued_since or self.sim_time for sid in q if sid in self.shipments]
            if vals:self.warehouse_first_arrival[warehouse]=min(vals)

    async def _process_order(self,order):
        sid=order["shipment_id"]
        if sid in self.shipments:return
        now=self.sim_time
        sh=Shipment(
            sid,order["pickup_city"],order["delivery_city"],float(order["weight_kg"]),float(order.get("volume_m3",1.0)),
            float(order.get("revenue_usd",500.0)),
            pd.Timestamp(order["pickup_deadline"]).to_pydatetime() if order.get("pickup_deadline") is not None else now+timedelta(minutes=150),
            pd.Timestamp(order["delivery_deadline"]).to_pydatetime() if order.get("delivery_deadline") is not None else now+timedelta(minutes=480),
            order.get("priority","Standard"),
            package_length_m=float(order.get("package_length_m",0.0)),package_width_m=float(order.get("package_width_m",0.0)),
            package_height_m=float(order.get("package_height_m",0.0)))
        sh.warehouse_city=nearest_warehouse(sh.pickup_city);self._baseline_plan(sh);self.shipments[sid]=sh
        active=[v for v in self.vehicles.values() if v.status=="enroute"];decision,stats=self.optimizer.evaluate(active,sh)
        if decision:
            v=decision["vehicle"];self._remove_from_warehouse_queue(sid,sh.warehouse_city);sh.status="assigned";sh.assigned_vehicle=v.vehicle_id;sh.assigned_at=self.sim_time
            v.assigned_shipments.append(sid);v.reserved_load_kg+=sh.weight_kg;v.reserved_volume_m3+=sh.volume_m3;v.status="enroute";v.stops=decision["stops"]
            sh.actual_cost_usd=round(max(0,decision["extra_km"])*v.cost_per_km,2);sh.cost_saving_usd=round(sh.baseline_cost_usd-sh.actual_cost_usd,2)
            self.metrics["allocated_actual_cost"]+=sh.actual_cost_usd;self.metrics["processed"]+=1;self.metrics["revenue"]+=sh.revenue_usd;self.metrics["consolidated"]+=1
            event={"type":"notification","shipment_id":sid,"decision":"consolidate","vehicle_id":v.vehicle_id,"warehouse":sh.warehouse_city,
                   "message":f"NOTIFICATION → {v.vehicle_id}: pickup {sh.warehouse_city}; load {sh.weight_kg:.0f} kg / {sh.volume_m3:.2f} m³; +{decision['extra_km']:.1f} km detour; estimated save USD {sh.cost_saving_usd:,.0f}; pickup ETA {decision.get('eta_min',0):.0f} min",
                   "optimizer_ms":stats["decision_ms"],"detour_pct":round(decision["detour_pct"],1)}
            self.history.append(event);self.history=self.history[-200:];await self.bus.publish(event);self._schedule_route(v);return

        self._queue_order(sh)
        event={"type":"warehouse","shipment_id":sid,"decision":"queued","warehouse":sh.warehouse_city,
               "message":f"QUEUE {sid} at {sh.warehouse_city}: {sh.weight_kg:.0f} kg / {sh.volume_m3:.2f} m³; waiting for full truck or 2-hour departure",
               "optimizer_ms":stats["decision_ms"]}
        self.history.append(event);self.history=self.history[-200:];await self.bus.publish(event)

    def _plan_batch_for_vehicle(self,warehouse,pending,vehicle):
        chosen=[];kg=0.0;vol=0.0
        for sh in pending:
            if kg+sh.weight_kg<=vehicle.capacity_kg+1e-6 and vol+sh.volume_m3<=vehicle.volume_capacity_m3+1e-6:
                chosen.append(sh);kg+=sh.weight_kg;vol+=sh.volume_m3
        if not chosen:return None
        remaining=list(chosen);order=[];current=warehouse
        while remaining:
            nxt=min(remaining,key=lambda sh:city_distance(current,sh.delivery_city));order.append(nxt.delivery_city);current=nxt.delivery_city;remaining.remove(nxt)
        route_km=route_distance([vehicle.current_city,warehouse]+order)
        while chosen and route_km>vehicle.max_distance_km:
            drop=chosen.pop()
            if drop.delivery_city in order:order.remove(drop.delivery_city)
            route_km=route_distance([vehicle.current_city,warehouse]+order)
        if not chosen:return None
        kg=sum(sh.weight_kg for sh in chosen);vol=sum(sh.volume_m3 for sh in chosen)
        load_pct=100*kg/vehicle.capacity_kg if vehicle.capacity_kg else 0;vol_pct=100*vol/vehicle.volume_capacity_m3 if vehicle.volume_capacity_m3 else 0
        full=max(load_pct,vol_pct)>=self.FULL_LOAD_PCT or len(chosen)<len(pending)
        return {"shipments":chosen,"destinations":order,"route_km":route_km,"full":full,"trigger_reason":"FULL" if full else "2H"}

    async def _dispatch_due_warehouses(self):
        for warehouse,ids in list(self.warehouse_queues.items()):
            pending=[self.shipments[sid] for sid in ids if sid in self.shipments and self.shipments[sid].status=="queued"]
            if not pending:
                self.warehouse_queues.pop(warehouse,None);self.warehouse_first_arrival.pop(warehouse,None);continue
            first=self.warehouse_first_arrival.get(warehouse,self.sim_time);due=(self.sim_time-first).total_seconds()/60>=self.WAREHOUSE_DEPARTURE_MIN
            candidates=[v for v in self.vehicles.values() if v.status=="idle"];best=None
            for v in sorted(candidates,key=lambda x:(x.capacity_kg,x.volume_capacity_m3)):
                plan=self._plan_batch_for_vehicle(warehouse,pending,v)
                if not plan:continue
                if due or plan["full"]:
                    best=(v,plan)
                    if plan["full"] and not due:break
            if not best:continue
            v,plan=best;batch=plan["shipments"];destinations=plan["destinations"];total_w=sum(s.weight_kg for s in batch);total_v=sum(s.volume_m3 for s in batch)
            route_cities=[v.current_city,warehouse]+destinations;route_km=route_distance(route_cities);trip_cost=route_km*v.cost_per_km
            stops=[Stop(warehouse,"pickup",[s.shipment_id for s in batch])];grouped={}
            for sh in batch:grouped.setdefault(sh.delivery_city,[]).append(sh.shipment_id)
            stops.extend(Stop(city,"delivery",sids) for city,sids in grouped.items())
            for sh in batch:
                share=0.7*sh.weight_kg/max(total_w,1.0)+0.3*sh.volume_m3/max(total_v,1e-6)
                sh.actual_cost_usd=round(trip_cost*share,2);sh.cost_saving_usd=round(sh.baseline_cost_usd-sh.actual_cost_usd,2)
                self.metrics["allocated_actual_cost"]+=sh.actual_cost_usd;self._remove_from_warehouse_queue(sh.shipment_id,warehouse);self.metrics["queued"]=max(0,self.metrics["queued"]-1)
                sh.status="assigned";sh.assigned_vehicle=v.vehicle_id;sh.assigned_at=self.sim_time;sh.queued_since=None
                v.assigned_shipments.append(sh.shipment_id);v.reserved_load_kg+=sh.weight_kg;v.reserved_volume_m3+=sh.volume_m3
            v.status="enroute";v.stops=stops;self.metrics["processed"]+=len(batch);self.metrics["dispatched"]+=1;self.metrics["warehouse_batched"]+=len(batch);self.metrics["warehouse_departures"]+=1
            event={"type":"departure","decision":"warehouse_batch","warehouse":warehouse,"vehicle_id":v.vehicle_id,"shipments":[s.shipment_id for s in batch],
                   "message":f"TRUCK DEPARTURE {v.vehicle_id} from {warehouse} [{plan['trigger_reason']}]: {len(batch)} shipment(s), {total_w:.0f}/{v.capacity_kg:.0f} kg, {total_v:.2f}/{v.volume_capacity_m3:.2f} m³, route {route_km:.0f} km, allocated trip USD {trip_cost:,.0f}"}
            self.history.append(event);self.history=self.history[-200:];await self.bus.publish(event);self._schedule_route(v)

    async def _retry_queued(self,limit=10):
        if limit<=0:return
        for sh in [s for s in self.shipments.values() if s.status=="queued"][:limit]:
            active=[v for v in self.vehicles.values() if v.status=="enroute"];decision,stats=self.optimizer.evaluate(active,sh)
            if not decision:continue
            v=decision["vehicle"];self._remove_from_warehouse_queue(sh.shipment_id,sh.warehouse_city);sh.status="assigned";sh.assigned_vehicle=v.vehicle_id;sh.assigned_at=self.sim_time
            v.assigned_shipments.append(sh.shipment_id);v.reserved_load_kg+=sh.weight_kg;v.reserved_volume_m3+=sh.volume_m3;v.status="enroute";v.stops=decision["stops"]
            sh.actual_cost_usd=round(max(0,decision["extra_km"])*v.cost_per_km,2);sh.cost_saving_usd=round(sh.baseline_cost_usd-sh.actual_cost_usd,2)
            self.metrics["queued"]=max(0,self.metrics["queued"]-1);self.metrics["processed"]+=1;self.metrics["consolidated"]+=1;self.metrics["allocated_actual_cost"]+=sh.actual_cost_usd;self.metrics["revenue"]+=sh.revenue_usd
            e={"type":"notification","shipment_id":sh.shipment_id,"decision":"reassign","vehicle_id":v.vehicle_id,"warehouse":sh.warehouse_city,
               "message":f"NOTIFICATION → {v.vehicle_id}: reassigned {sh.shipment_id} from {sh.warehouse_city}; estimated save USD {sh.cost_saving_usd:,.0f}","optimizer_ms":stats["decision_ms"]}
            self.history.append(e);self.history=self.history[-200:];await self.bus.publish(e);self._schedule_route(v)

    async def _advance_vehicles(self,sim_seconds):
        for v in self.vehicles.values():
            if v.status!="enroute" or not v.geometry:continue
            old_progress=v.progress_km;step_km=v.speed_kmh*sim_seconds/3600*max(0.25,self.traffic_factor);v.progress_km=min(v.route_total_km,v.progress_km+step_km)
            traveled=max(0.0,v.progress_km-old_progress);v.total_distance_km+=traveled;self.metrics["km"]+=traveled;self.metrics["cost"]+=traveled*v.cost_per_km
            idx=0
            while idx<len(v.geometry_cum_km)-1 and v.geometry_cum_km[idx+1]<v.progress_km:idx+=1
            if idx<len(v.geometry)-1:
                a,b=v.geometry[idx],v.geometry[idx+1];span=v.geometry_cum_km[idx+1]-v.geometry_cum_km[idx];f=0 if span<=0 else (v.progress_km-v.geometry_cum_km[idx])/span
                v.current_lat=a[0]+f*(b[0]-a[0]);v.current_lon=a[1]+f*(b[1]-a[1]);v.current_city=nearest_city(v.current_lat,v.current_lon)
            elif v.geometry:v.current_lat,v.current_lon=v.geometry[-1]
            changed=False
            for s in v.stops:
                if s.completed or s.cumulative_km>v.progress_km+1e-6:continue
                s.completed=True;changed=True
                for sid in s.shipment_ids:
                    sh=self.shipments.get(sid)
                    if not sh:continue
                    if s.kind=="pickup" and sh.status=="assigned":
                        sh.status="picked_up";v.reserved_load_kg=max(0,v.reserved_load_kg-sh.weight_kg);v.reserved_volume_m3=max(0,v.reserved_volume_m3-sh.volume_m3);v.current_load_kg+=sh.weight_kg;v.current_volume_m3+=sh.volume_m3
                    elif s.kind=="delivery" and sh.status in ("picked_up","assigned"):
                        sh.status="delivered";self.metrics["delivered"]+=1;v.current_load_kg=max(0,v.current_load_kg-sh.weight_kg);v.current_volume_m3=max(0,v.current_volume_m3-sh.volume_m3);v.assigned_shipments=[x for x in v.assigned_shipments if x!=sid]
                        sh.time_saving_min=round((sh.baseline_eta-self.sim_time).total_seconds()/60,2) if sh.baseline_eta else 0.0;self.metrics["time_saved_min"]+=sh.time_saving_min
                        e={"type":"delivery","shipment_id":sid,"vehicle_id":v.vehicle_id,"message":f"DELIVERED {sid} via {v.vehicle_id}: time saving {sh.time_saving_min:+.1f} min, cost saving USD {sh.cost_saving_usd:+,.0f}"}
                        self.history.append(e);self.history=self.history[-200:];await self.bus.publish(e)
            if changed:
                v.stops=[s for s in v.stops if not s.completed]
                if not v.stops:v.status="idle";v.progress_km=0;v.geometry=[];v.geometry_cum_km=[];v.route_total_km=0;v.route_quality="none"

    def _schedule_route(self,v):
        old=self.route_tasks_by_vehicle.get(v.vehicle_id)
        if old and not old.done():old.cancel()
        v.route_version+=1;token=v.route_version
        async def task():
            points=[(v.current_lat,v.current_lon)]+[coords(s.city) for s in v.stops if not s.completed]
            t=time.perf_counter();result=await self.router.route(points);ms=(time.perf_counter()-t)*1000
            async with self.lock:
                if token!=v.route_version or v.status=="broken":return
                v.geometry=result["geometry"];v.route_total_km=float(result["distance_km"]);v.geometry_cum_km=[];cum=0.0
                for i,p in enumerate(v.geometry):
                    if i:
                        from .geo import haversine_km
                        cum+=haversine_km(v.geometry[i-1],p)
                    v.geometry_cum_km.append(cum)
                v.progress_km=0.0;v.route_quality=result["quality"];v.last_route_ms=ms;legcum=0.0
                for idx,s in enumerate([s for s in v.stops if not s.completed]):
                    if idx<len(result.get("legs_km",[])):legcum+=result["legs_km"][idx]
                    s.cumulative_km=legcum
            await self.bus.publish({"type":"route","vehicle_id":v.vehicle_id,"quality":result["quality"],"route_ms":ms,"distance_km":result["distance_km"]})
        t=asyncio.create_task(task());self.route_tasks.add(t);self.route_tasks_by_vehicle[v.vehicle_id]=t;t.add_done_callback(self.route_tasks.discard)

    async def submit_live_order(self,order):
        await self.live_queue.put(order);await self.bus.publish({"type":"ingest","shipment_id":order.get("shipment_id"),"message":f"LIVE ORDER INGESTED {order.get('shipment_id')}"})

    async def set_traffic(self,factor):
        self.traffic_factor=factor;await self.bus.publish({"type":"network","traffic_factor":factor,"message":f"NETWORK TRAFFIC FACTOR {factor:.2f}x"})

    async def trigger_breakdown(self,vehicle_id):
        v=self.vehicles.get(vehicle_id)
        if not v:return False
        v.status="broken";self.metrics["breakdowns"]+=1;affected=set(v.assigned_shipments)
        for sid in affected:
            sh=self.shipments.get(sid)
            if sh and sh.status!="delivered":
                sh.status="queued";sh.assigned_vehicle=None;sh.queued_since=self.sim_time
                if sh.warehouse_city:
                    self.warehouse_queues.setdefault(sh.warehouse_city,[]).append(sid);self.warehouse_first_arrival.setdefault(sh.warehouse_city,self.sim_time)
                self.metrics["queued"]+=1
        v.assigned_shipments=[];v.current_load_kg=0;v.reserved_load_kg=0;v.current_volume_m3=0;v.reserved_volume_m3=0;v.stops=[];v.geometry=[]
        event={"type":"disruption","kind":"breakdown","vehicle_id":vehicle_id,"message":f"BREAKDOWN {vehicle_id}; {len(affected)} shipment(s) returned to nearest warehouse queue"}
        self.history.append(event);self.history=self.history[-200:];await self.bus.publish(event);return True

    async def recover_vehicle(self,vehicle_id):
        v=self.vehicles.get(vehicle_id)
        if not v:return False
        v.status="idle";await self.bus.publish({"type":"disruption","kind":"repair","vehicle_id":vehicle_id,"message":f"REPAIRED {vehicle_id}"});return True

    async def pause(self):
        self.running=False;await self.bus.publish({"type":"engine","action":"paused"})

    async def resume(self):
        await self.start();await self.bus.publish({"type":"engine","action":"resumed"})

    def snapshot(self):
        now=self.sim_time
        at_risk=sum(1 for sh in self.shipments.values() if sh.status in ("assigned","picked_up") and sh.delivery_deadline<now+timedelta(minutes=30))
        cost_savings=self.metrics["baseline_cost"]-self.metrics["allocated_actual_cost"]
        avg_time_saved=self.metrics["time_saved_min"]/max(1,self.metrics["delivered"])
        warehouse_rows=[]
        warehouse_ids=self.cities[self.cities["type"]=="hub"]["city_id"].tolist()

        for wh in warehouse_ids:
            ids=self.warehouse_queues.get(wh,[])
            live=[self.shipments[sid] for sid in ids if sid in self.shipments and self.shipments[sid].status=="queued"]
            oldest=min((s.queued_since or now) for s in live) if live else None
            wait=(now-oldest).total_seconds()/60 if oldest else 0.0
            available=[v.vehicle_id for v in self.vehicles.values() if v.status=="idle" and v.current_city==wh]
            departure_deadline=(oldest+timedelta(minutes=self.WAREHOUSE_DEPARTURE_MIN)).isoformat() if oldest else None
            warehouse_rows.append({
                "warehouse":wh,
                "orders":len(live),
                "queue_depth":len(live),
                "weight_kg":round(sum(s.weight_kg for s in live),1),
                "volume_m3":round(sum(s.volume_m3 for s in live),2),
                "oldest_wait_min":round(wait,1),
                "departure_due":bool(wait>=self.WAREHOUSE_DEPARTURE_MIN),
                "departure_deadline":departure_deadline,
                "departure_in_min":round(max(0.0,self.WAREHOUSE_DEPARTURE_MIN-wait),1) if live else None,
                "available_vehicle_count":len(available),
                "available_vehicles":available
            })

        return {"sim_time":self.sim_time.isoformat(),"running":self.running,"speed":self.speed,"orders_processed":self.metrics["processed"],"orders_total":len(self.orders),
                "queue_depth":sum(1 for s in self.shipments.values() if s.status=="queued"),
                "metrics":{**self.metrics,"consolidation_rate":round(100*self.metrics["consolidated"]/max(1,self.metrics["processed"]),1),
                            "fleet_utilization":round(sum(v.utilization_pct for v in self.vehicles.values())/max(1,len(self.vehicles)),1),"at_risk":at_risk,
                            "optimizer_p95_ms":self.optimizer.p95_ms(),"route_requests":self.router.requests,"real_routes":self.router.real_routes,"fallback_routes":self.router.fallback_routes,
                            "cost_savings_usd":round(cost_savings,2),"cost_savings_pct":round(100*cost_savings/max(1,self.metrics["baseline_cost"]),1),
                            "avg_time_saved_min":round(avg_time_saved,1),"dispatches_avoided":max(0,self.metrics["processed"]-self.metrics["dispatched"])},
                "vehicles":[v.snapshot() for v in self.vehicles.values()],"warehouses":warehouse_rows,"history":self.history[-50:],
                "router":{"url":self.router.base_url,"last_error":self.router.last_error,"real_ratio":round(100*self.router.real_routes/max(1,self.router.real_routes+self.router.fallback_routes),1),
                          "circuit_open":self.router.circuit_open,"retry_in_s":max(0,round(self.router.open_until-time.time(),1)) if self.router.circuit_open else 0},
                "live_stream":{"enabled":self._live_stream_enabled,"interval_s":self._live_stream_interval}}
