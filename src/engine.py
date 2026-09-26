from __future__ import annotations
import asyncio
import time
from dataclasses import asdict
from datetime import datetime,timedelta
import pandas as pd

from .config import settings
from .geo import coords,nearest_city,nearest_warehouse,city_distance,route_distance
from .models import Shipment,Vehicle,Stop,vehicle_from_dict
from .optimizer import Optimizer
from .event_bus import EventBus
from .router import RoadRouter
from .forecast import DemandForecaster
from .global_optimizer import GlobalFleetOptimizer
from . import persistence


class Engine:
    WAREHOUSE_DEPARTURE_MIN=120.0
    FULL_LOAD_PCT=90.0
    GLOBAL_REOPT_MIN=5.0
    OFFER_AUTO_ACCEPT_S=5.0

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
            self.orders=pd.DataFrame(columns=["shipment_id","arrival_time","pickup_city","delivery_city","weight_kg","volume_m3",
                                              "package_length_m","package_width_m","package_height_m","revenue_usd",
                                              "pickup_deadline","delivery_deadline","priority"])

        self.router=RoadRouter()
        self.bus=EventBus(settings.redis_url)
        self.optimizer=Optimizer(self.router)
        self.forecaster=DemandForecaster()
        self.global_optimizer=GlobalFleetOptimizer()

        self.lock=asyncio.Lock()
        self.task=None
        self.running=False
        self.speed=settings.sim_minutes_per_second
        self.idx=0
        self.sim_time=self.orders.iloc[0].arrival_time.to_pydatetime() if len(self.orders) else datetime.now()

        self.shipments={}
        self.vehicles={}
        self.history=[]
        self.event_seq=0
        self.route_tasks=set()
        self.route_tasks_by_vehicle={}
        self.live_queue=asyncio.Queue()
        self.traffic_factor=settings.traffic_factor

        self.warehouse_queues={}
        self.warehouse_first_arrival={}
        self.driver_offers={}
        self.offer_counter=0
        self.offer_tasks={}
        self.reposition_plan=[]
        self.last_global_opt_sim=self.sim_time

        self.metrics={
            "received":0,"processed":0,"consolidated":0,"warehouse_batched":0,"dispatched":0,"queued":0,"delivered":0,
            "breakdowns":0,"km":0.0,"cost":0.0,"revenue":0.0,
            "baseline_cost":0.0,"baseline_km":0.0,"baseline_time_min":0.0,
            "allocated_actual_cost":0.0,"time_saved_min":0.0,"warehouse_departures":0,
            "empty_km":0.0,"backhaul_shipments":0,"backhaul_km_saved":0.0,
            "driver_accepts":0,"driver_rejects":0,"breakdown_transfers":0,"reposition_km":0.0,
            "on_time_deliveries":0
        }

        self._build_fleet()
        self._last_checkpoint_at=0.0
        self._checkpoint_task=None
        self._live_stream_enabled=settings.live_order_stream
        self._live_stream_interval=settings.live_order_interval_s
        self._live_stream_task=None
        self._live_sampler=None

    def _record(self,event):
        event=dict(event)
        self.event_seq+=1
        event.setdefault("seq",self.event_seq)
        event.setdefault("timestamp",self.sim_time.isoformat())
        self.history.append(event)
        self.history=self.history[-500:]
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
                vehicle_id=r.vehicle_id,vehicle_type=r.vehicle_type,
                capacity_kg=float(r.capacity_kg),max_distance_km=float(r.max_distance_km),
                cost_per_km=float(r.cost_per_km),speed_kmh=speeds.get(r.vehicle_type,62),
                base_city=r.base_city,current_lat=lat,current_lon=lon,current_city=r.base_city,
                volume_capacity_m3=volume_caps.get(r.vehicle_type,28.0))

    async def restore(self):
        try:data=await asyncio.to_thread(persistence.load_latest_checkpoint)
        except Exception as e:
            await self._emit({"type":"error","message":f"checkpoint load failed: {e}"});return
        if not data:return
        self.idx=data.get("idx",self.idx)
        self.sim_time=datetime.fromisoformat(data["sim_time"]) if data.get("sim_time") else self.sim_time
        self.traffic_factor=data.get("traffic_factor",self.traffic_factor)
        self.metrics={**self.metrics,**data.get("metrics",{})}
        self.shipments={sid:Shipment.from_dict(sd) for sid,sd in data.get("shipments",{}).items()}
        if data.get("vehicles"):self.vehicles={vid:vehicle_from_dict(vd) for vid,vd in data["vehicles"].items()}
        self.history=data.get("history",self.history)
        self.event_seq=max((int(e.get("seq",0)) for e in self.history),default=0)
        for sh in self.shipments.values():
            if sh.status=="queued":
                queue_city=sh.handoff_city if hasattr(sh,"handoff_city") and sh.handoff_city else sh.warehouse_city
                if queue_city:
                    self.warehouse_queues.setdefault(queue_city,[]).append(sh.shipment_id)
                    self.warehouse_first_arrival.setdefault(queue_city,sh.queued_since or self.sim_time)

    def _serialize_state(self):
        return {"idx":self.idx,"sim_time":self.sim_time.isoformat(),"traffic_factor":self.traffic_factor,
                "metrics":self.metrics,"shipments":{sid:s.to_dict() for sid,s in self.shipments.items()},
                "vehicles":{vid:asdict(v) for vid,v in self.vehicles.items()},"history":self.history[-500:]}

    async def _maybe_checkpoint(self):
        if settings.checkpoint_interval_s<=0:return
        now=time.time()
        if now-self._last_checkpoint_at<settings.checkpoint_interval_s:return
        if self._checkpoint_task and not self._checkpoint_task.done():return
        self._last_checkpoint_at=now
        state=self._serialize_state()
        async def save():
            try:await asyncio.to_thread(persistence.save_checkpoint,state)
            except Exception as e:await self._emit({"type":"error","message":f"checkpoint save failed: {e}"})
        self._checkpoint_task=asyncio.create_task(save())

    async def start(self):
        await self.router.start();await self.bus.start()
        if not self.task or self.task.done():
            self.running=True
            self.task=asyncio.create_task(self._loop())
            await self._emit({"type":"engine","action":"started"})
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
        await self._emit({"type":"engine","action":"live_stream","enabled":enabled,"interval_s":self._live_stream_interval,
                          "message":f"Live order stream {'enabled — one order every '+str(int(self._live_stream_interval))+'s' if enabled else 'disabled'}"})

    async def stop(self):
        self.running=False
        if self.task:
            try:await asyncio.wait_for(self.task,timeout=2)
            except asyncio.TimeoutError:self.task.cancel()
        if self._live_stream_task and not self._live_stream_task.done():self._live_stream_task.cancel()
        for t in self.offer_tasks.values():
            if not t.done():t.cancel()
        try:await asyncio.to_thread(persistence.save_checkpoint,self._serialize_state())
        except Exception:pass
        await self.router.close();await self.bus.close()

    async def reset(self):
        async with self.lock:
            self.idx=0
            self.sim_time=self.orders.iloc[0].arrival_time.to_pydatetime() if len(self.orders) else datetime.now()
            self.shipments={};self.history=[];self.event_seq=0;self.warehouse_queues={};self.warehouse_first_arrival={}
            self.driver_offers={};self.offer_counter=0;self.reposition_plan=[];self.last_global_opt_sim=self.sim_time
            self.metrics={k:0.0 if isinstance(v,float) else 0 for k,v in self.metrics.items()}
            self.vehicles={};self._build_fleet()
        await self._emit({"type":"engine","action":"reset"})

    async def _loop(self):
        last=time.perf_counter()
        while self.running:
            now=time.perf_counter();wall_dt=min(0.5,now-last);last=now
            try:
                await self.tick(wall_dt)
                await self._maybe_checkpoint()
            except Exception as e:
                await self._emit({"type":"error","message":str(e)})
            await asyncio.sleep(settings.engine_tick_ms/1000)

    async def tick(self,wall_dt):
        sim_dt=wall_dt*self.speed*60
        self.sim_time+=timedelta(seconds=sim_dt)
        await self._advance_vehicles(sim_dt)

        count=0
        while not self.live_queue.empty() and count<settings.max_orders_per_tick:
            o=await self.live_queue.get();o.setdefault("arrival_time",self.sim_time);await self._process_order(o);count+=1
        while self.idx<len(self.orders) and pd.Timestamp(self.orders.iloc[self.idx].arrival_time).to_pydatetime()<=self.sim_time and count<settings.max_orders_per_tick:
            o=self.orders.iloc[self.idx].to_dict();self.idx+=1;await self._process_order(o);count+=1

        await self._retry_queued(limit=min(12,max(0,settings.max_orders_per_tick-count)))
        await self._dispatch_due_warehouses()
        await self._maybe_global_reposition()

    def _service_times(self,weight_kg):
        return min(35.0,12.0+weight_kg/1500.0),min(30.0,10.0+weight_kg/1800.0)

    def _baseline_plan(self,sh):
        warehouse=sh.warehouse_city or sh.pickup_city
        candidates=[v for v in self.vehicles.values() if v.capacity_kg>=sh.weight_kg and v.volume_capacity_m3>=sh.volume_m3]
        if not candidates:candidates=[v for v in self.vehicles.values() if v.capacity_kg>=sh.weight_kg]
        if not candidates:candidates=list(self.vehicles.values())
        if not candidates:return
        best=min(candidates,key=lambda v:(v.cost_per_km,v.capacity_kg))
        dist=city_distance(warehouse,sh.delivery_city)
        pickup_s,delivery_s=sh.pickup_service_min,sh.delivery_service_min
        travel=dist/max(1.0,best.speed_kmh)*60
        sh.baseline_distance_km=round(dist,2)
        sh.baseline_cost_usd=round(dist*best.cost_per_km,2)
        sh.baseline_time_min=round(travel+pickup_s+delivery_s,2)
        sh.baseline_eta=self.sim_time+timedelta(minutes=sh.baseline_time_min)
        self.metrics["baseline_cost"]+=sh.baseline_cost_usd
        self.metrics["baseline_km"]+=dist
        self.metrics["baseline_time_min"]+=sh.baseline_time_min

    def _queue_city(self,sh):
        return getattr(sh,"handoff_city",None) or sh.warehouse_city

    def _queue_order(self,sh):
        wh=self._queue_city(sh)
        if not wh:return
        self.warehouse_queues.setdefault(wh,[]).append(sh.shipment_id)
        sh.status="queued";sh.queued_since=sh.queued_since or self.sim_time
        self.warehouse_first_arrival.setdefault(wh,sh.queued_since)
        self.metrics["queued"]+=1

    def _remove_from_warehouse_queue(self,sid,warehouse=None):
        wh=warehouse or self._queue_city(self.shipments.get(sid))
        if not wh:return
        q=self.warehouse_queues.get(wh,[])
        if sid in q:q.remove(sid)
        if not q:self.warehouse_queues.pop(wh,None);self.warehouse_first_arrival.pop(wh,None)
        else:
            vals=[self.shipments[x].queued_since or self.sim_time for x in q if x in self.shipments]
            if vals:self.warehouse_first_arrival[wh]=min(vals)

    def _build_shipment(self,order):
        now=self.sim_time;ps,ds=self._service_times(float(order["weight_kg"]))
        sh=Shipment(
            order["shipment_id"],order["pickup_city"],order["delivery_city"],float(order["weight_kg"]),
            float(order.get("volume_m3",1.0)),float(order.get("revenue_usd",500.0)),
            pd.Timestamp(order["pickup_deadline"]).to_pydatetime() if order.get("pickup_deadline") is not None else now+timedelta(minutes=150),
            pd.Timestamp(order["delivery_deadline"]).to_pydatetime() if order.get("delivery_deadline") is not None else now+timedelta(minutes=480),
            order.get("priority","Standard"),
            package_length_m=float(order.get("package_length_m",0.0)),
            package_width_m=float(order.get("package_width_m",0.0)),
            package_height_m=float(order.get("package_height_m",0.0)),
            pickup_service_min=ps,delivery_service_min=ds,
            order_arrival_at=pd.Timestamp(order.get("arrival_time",now)).to_pydatetime() if order.get("arrival_time") is not None else now)
        sh.warehouse_city=nearest_warehouse(sh.pickup_city)
        sh.original_warehouse_city=sh.warehouse_city
        return sh

    async def _process_order(self,order):
        sid=order["shipment_id"]
        if sid in self.shipments:return
        sh=self._build_shipment(order)
        self.shipments[sid]=sh
        self.metrics["received"]+=1
        self.forecaster.observe(sh.warehouse_city,self.sim_time,sh.weight_kg,sh.volume_m3)
        self._baseline_plan(sh)

        active=[v for v in self.vehicles.values() if v.status=="enroute" and v.mission=="freight"]
        decision,stats=await self.optimizer.evaluate_async(active,sh,self.sim_time)

        if decision:
            await self._assign_active(sh,decision,stats,decision_type="consolidate")
            return

        self._queue_order(sh)
        await self._emit({"type":"warehouse","shipment_id":sid,"decision":"queued","warehouse":self._queue_city(sh),
                          "message":f"QUEUE {sid} at {self._queue_city(sh)}: {sh.weight_kg:.0f} kg / {sh.volume_m3:.2f} m³; waiting for full truck or 2-hour departure",
                          "optimizer_ms":stats["decision_ms"],"rejections":stats.get("rejections",[])})

    async def _assign_active(self,sh,decision,stats,decision_type="consolidate"):
        v=decision["vehicle"]
        was_queued=sh.status=="queued"
        self._remove_from_warehouse_queue(sh.shipment_id)
        if was_queued:self.metrics["queued"]=max(0,self.metrics["queued"]-1)
        sh.status="assigned";sh.assigned_vehicle=v.vehicle_id;sh.assigned_at=self.sim_time
        sh.adaptive_distance_km=round(max(0.0,decision["extra_km"]),2)
        sh.actual_cost_usd=round(max(0.0,decision["extra_km"])*v.cost_per_km,2)
        sh.cost_saving_usd=round(sh.baseline_cost_usd-sh.actual_cost_usd,2)
        sh.adaptive_time_min=round(decision.get("eta_min",0.0),2)
        sh.time_saving_min=round(sh.baseline_time_min-sh.adaptive_time_min,2)
        sh.is_backhaul=bool(decision.get("backhaul",False))
        sh.decision_reason=decision.get("reason","")
        sh.decision_score=decision.get("score",0.0)
        v.assigned_shipments.append(sh.shipment_id)
        v.reserved_load_kg+=sh.weight_kg;v.reserved_volume_m3+=sh.volume_m3
        v.status="enroute";v.mission="freight";v.stops=decision["stops"]
        self.metrics["allocated_actual_cost"]+=sh.actual_cost_usd
        if not was_queued:
            self.metrics["processed"]+=1
            self.metrics["revenue"]+=sh.revenue_usd
        if decision_type in ("consolidate","reassign"):self.metrics["consolidated"]+=1
        if sh.is_backhaul:
            self.metrics["backhaul_shipments"]+=1
            self.metrics["backhaul_km_saved"]+=max(0.0,city_distance(sh.warehouse_city,v.base_city)-city_distance(sh.delivery_city,v.base_city))

        offer_id=await self._create_driver_offer(sh,v,decision,stats,decision_type)
        await self._emit({"type":"notification","offer_id":offer_id,"shipment_id":sh.shipment_id,"decision":decision_type,
                          "vehicle_id":v.vehicle_id,"warehouse":sh.warehouse_city,
                          "message":f"DRIVER OFFER → {v.vehicle_id}: pickup {sh.warehouse_city}; {sh.weight_kg:.0f} kg / {sh.volume_m3:.2f} m³; +{decision['extra_km']:.1f} km; estimated saving USD {sh.cost_saving_usd:,.0f}; ETA {decision.get('pickup_eta_min',0):.0f} min",
                          "optimizer_ms":stats["decision_ms"],"detour_pct":round(decision["detour_pct"],1),
                          "why":decision.get("reason",""),"rejections":decision.get("rejections",[])})
        self._schedule_route(v)

    async def _create_driver_offer(self,sh,v,decision,stats,decision_type):
        self.offer_counter+=1
        oid=f"OFF-{self.offer_counter:06d}"
        offer={"offer_id":oid,"shipment_id":sh.shipment_id,"vehicle_id":v.vehicle_id,"warehouse":sh.warehouse_city,
               "status":"pending","created_at":self.sim_time.isoformat(),"reason":decision.get("reason",""),
               "score":round(decision.get("score",0.0),1),"detour_pct":round(decision.get("detour_pct",0.0),1),
               "incremental_km":round(decision.get("extra_km",0.0),1),"pickup_eta_min":round(decision.get("pickup_eta_min",0.0),1),
               "savings_usd":round(sh.cost_saving_usd,2),"sla_buffer_min":round(decision.get("sla_buffer_min",0.0),1),
               "rejections":decision.get("rejections",[])[:6],"decision_type":decision_type}
        self.driver_offers[oid]=offer
        async def auto_accept():
            await asyncio.sleep(self.OFFER_AUTO_ACCEPT_S)
            if self.driver_offers.get(oid,{}).get("status")=="pending":
                await self.respond_driver_offer(oid,True,auto=True)
        self.offer_tasks[oid]=asyncio.create_task(auto_accept())
        return oid

    async def respond_driver_offer(self,offer_id,accepted:bool,auto:bool=False):
        offer=self.driver_offers.get(offer_id)
        if not offer:return False
        if offer["status"]!="pending":return True
        sid=offer["shipment_id"];sh=self.shipments.get(sid);v=self.vehicles.get(offer["vehicle_id"])
        if accepted:
            offer["status"]="accepted";offer["accepted_at"]=self.sim_time.isoformat()
            self.metrics["driver_accepts"]+=1
            await self._emit({"type":"driver","offer_id":offer_id,"shipment_id":sid,"vehicle_id":offer["vehicle_id"],
                              "decision":"accepted","message":f"DRIVER ACCEPTED {offer_id} — {offer['vehicle_id']} will collect at {offer['warehouse']}{' (auto)' if auto else ''}"})
            return True
        if not sh or not v or sh.status not in ("assigned","queued"):
            offer["status"]="expired";return False
        offer["status"]="rejected";offer["rejected_at"]=self.sim_time.isoformat();self.metrics["driver_rejects"]+=1
        if sh.assigned_vehicle==v.vehicle_id:
            v.assigned_shipments=[x for x in v.assigned_shipments if x!=sid]
            v.reserved_load_kg=max(0,v.reserved_load_kg-sh.weight_kg);v.reserved_volume_m3=max(0,v.reserved_volume_m3-sh.volume_m3)
            v.stops=[stop for stop in v.stops if sid not in stop.shipment_ids]
            sh.assigned_vehicle=None;sh.status="queued";sh.queued_since=self.sim_time
            self.metrics["consolidated"]=max(0,self.metrics["consolidated"]-1)
            self.metrics["allocated_actual_cost"]=max(0.0,self.metrics["allocated_actual_cost"]-sh.actual_cost_usd)
            sh.actual_cost_usd=0.0;sh.adaptive_distance_km=0.0;sh.adaptive_time_min=0.0;sh.cost_saving_usd=0.0;sh.time_saving_min=0.0
            self._queue_order(sh);self._schedule_route(v)
        await self._emit({"type":"driver","offer_id":offer_id,"shipment_id":sid,"vehicle_id":offer["vehicle_id"],
                          "decision":"rejected","message":f"DRIVER REJECTED {offer_id} — optimizer will find the next feasible move"})
        return True

    def _plan_batch_for_vehicle(self,warehouse,pending,vehicle):
        chosen=[];kg=0.0;vol=0.0
        for sh in pending:
            if kg+sh.weight_kg<=vehicle.capacity_kg+1e-6 and vol+sh.volume_m3<=vehicle.volume_capacity_m3+1e-6:
                chosen.append(sh);kg+=sh.weight_kg;vol+=sh.volume_m3
        if not chosen:return None
        remaining=list(chosen);order=[];current=warehouse
        while remaining:
            nxt=min(remaining,key=lambda sh:city_distance(current,sh.delivery_city))
            order.append(nxt.delivery_city);current=nxt.delivery_city;remaining.remove(nxt)
        route_km=route_distance([vehicle.current_city,warehouse]+order)
        while chosen and route_km>vehicle.max_distance_km:
            drop=chosen.pop()
            if drop.delivery_city in order:order.remove(drop.delivery_city)
            route_km=route_distance([vehicle.current_city,warehouse]+order)
        if not chosen:return None
        kg=sum(sh.weight_kg for sh in chosen);vol=sum(sh.volume_m3 for sh in chosen)
        load_pct=100*kg/vehicle.capacity_kg if vehicle.capacity_kg else 0
        vol_pct=100*vol/vehicle.volume_capacity_m3 if vehicle.volume_capacity_m3 else 0
        full=max(load_pct,vol_pct)>=self.FULL_LOAD_PCT or len(chosen)<len(pending)
        return {"shipments":chosen,"destinations":order,"route_km":route_km,"full":full,"trigger_reason":"FULL" if full else "2H"}

    async def _dispatch_due_warehouses(self):
        for warehouse,ids in list(self.warehouse_queues.items()):
            pending=[self.shipments[sid] for sid in ids if sid in self.shipments and self.shipments[sid].status=="queued"]
            if not pending:continue
            first=self.warehouse_first_arrival.get(warehouse,self.sim_time)
            due=(self.sim_time-first).total_seconds()/60>=self.WAREHOUSE_DEPARTURE_MIN
            candidates=[v for v in self.vehicles.values() if v.status=="idle" and v.capacity_kg>=min(s.weight_kg for s in pending)]
            best=None
            for v in sorted(candidates,key=lambda x:(x.capacity_kg,x.volume_capacity_m3)):
                plan=self._plan_batch_for_vehicle(warehouse,pending,v)
                if plan and (due or plan["full"]):
                    best=(v,plan)
                    if plan["full"] and not due:break
            if not best:continue

            v,plan=best;batch=plan["shipments"];destinations=plan["destinations"]
            total_w=sum(s.weight_kg for s in batch);total_v=sum(s.volume_m3 for s in batch)
            route_cities=[v.current_city,warehouse]+destinations
            route_km=plan["route_km"];trip_cost=route_km*v.cost_per_km
            stops=[Stop(warehouse,"pickup",[s.shipment_id for s in batch],service_minutes=15.0+total_w/2500.0)]
            grouped={}
            for sh in batch:grouped.setdefault(sh.delivery_city,[]).append(sh.shipment_id)
            for city,sids in grouped.items():
                svc=sum(self.shipments[sid].delivery_service_min for sid in sids if sid in self.shipments)
                stops.append(Stop(city,"delivery",sids,service_minutes=svc))

            for sh in batch:
                share=0.7*sh.weight_kg/max(total_w,1.0)+0.3*sh.volume_m3/max(total_v,1e-6)
                sh.actual_cost_usd=round(trip_cost*share,2)
                sh.adaptive_distance_km=round(route_km,2)
                sh.cost_saving_usd=round(sh.baseline_cost_usd-sh.actual_cost_usd,2)
                sh.is_backhaul=city_distance(sh.delivery_city,v.base_city)<city_distance(warehouse,v.base_city)
                if sh.is_backhaul:
                    self.metrics["backhaul_shipments"]+=1
                    self.metrics["backhaul_km_saved"]+=max(0.0,city_distance(warehouse,v.base_city)-city_distance(sh.delivery_city,v.base_city))
                sh.adaptive_time_min=round(route_km/max(1.0,v.speed_kmh)*60+sum(s.service_minutes for s in stops),2)
                sh.time_saving_min=round(sh.baseline_time_min-sh.adaptive_time_min,2)
                sh.status="assigned";sh.assigned_vehicle=v.vehicle_id;sh.assigned_at=self.sim_time;sh.queued_since=None
                self.metrics["allocated_actual_cost"]+=sh.actual_cost_usd
                self._remove_from_warehouse_queue(sh.shipment_id,warehouse);self.metrics["queued"]=max(0,self.metrics["queued"]-1)
                v.assigned_shipments.append(sh.shipment_id);v.reserved_load_kg+=sh.weight_kg;v.reserved_volume_m3+=sh.volume_m3
                self.metrics["processed"]+=1;self.metrics["revenue"]+=sh.revenue_usd

            v.status="enroute";v.mission="freight";v.stops=stops
            self.metrics["dispatched"]+=1;self.metrics["warehouse_batched"]+=len(batch);self.metrics["warehouse_departures"]+=1
            await self._emit({"type":"departure","decision":"warehouse_batch","warehouse":warehouse,"vehicle_id":v.vehicle_id,
                              "shipments":[s.shipment_id for s in batch],
                              "message":f"TRUCK DEPARTURE {v.vehicle_id} from {warehouse} [{plan['trigger_reason']}]: {len(batch)} shipment(s), {total_w:.0f}/{v.capacity_kg:.0f} kg, {total_v:.2f}/{v.volume_capacity_m3:.2f} m³, route {route_km:.0f} km, trip USD {trip_cost:,.0f}"})
            self._schedule_route(v)

    async def _retry_queued(self,limit=12):
        if limit<=0:return
        queued=[s for s in self.shipments.values() if s.status=="queued"]
        for sh in queued[:limit]:
            active=[v for v in self.vehicles.values() if v.status=="enroute" and v.mission=="freight"]
            decision,stats=await self.optimizer.evaluate_async(active,sh,self.sim_time)
            if decision:
                await self._assign_active(sh,decision,stats,decision_type="reassign")
                continue
            if getattr(sh,"handoff_city",None):
                idle=[v for v in self.vehicles.values() if v.status=="idle" and v.capacity_kg>=sh.weight_kg and v.remaining_volume_m3>=sh.volume_m3]
                if idle:
                    v=min(idle,key=lambda x:city_distance(x.current_city,sh.handoff_city))
                    route_km=route_distance([v.current_city,sh.handoff_city,sh.delivery_city])
                    if route_km<=v.max_distance_km:
                        self._remove_from_warehouse_queue(sh.shipment_id,sh.handoff_city)
                        sh.status="assigned";sh.assigned_vehicle=v.vehicle_id;sh.assigned_at=self.sim_time
                        sh.actual_cost_usd=round(route_km*v.cost_per_km,2);sh.cost_saving_usd=round(sh.baseline_cost_usd-sh.actual_cost_usd,2)
                        sh.adaptive_distance_km=route_km;sh.adaptive_time_min=route_km/max(1.0,v.speed_kmh)*60+sh.pickup_service_min+sh.delivery_service_min
                        sh.time_saving_min=sh.baseline_time_min-sh.adaptive_time_min
                        v.assigned_shipments.append(sh.shipment_id);v.reserved_load_kg+=sh.weight_kg;v.reserved_volume_m3+=sh.volume_m3;v.status="enroute";v.mission="freight"
                        v.stops=[Stop(sh.handoff_city,"handoff",[sh.shipment_id],service_minutes=8.0),Stop(sh.delivery_city,"delivery",[sh.shipment_id],service_minutes=sh.delivery_service_min)]
                        self.metrics["queued"]=max(0,self.metrics["queued"]-1);self.metrics["processed"]+=1;self.metrics["allocated_actual_cost"]+=sh.actual_cost_usd;self.metrics["revenue"]+=sh.revenue_usd;self.metrics["breakdown_transfers"]+=1
                        sh.decision_reason=f"Emergency cargo transfer from {sh.handoff_city}"
                        await self._emit({"type":"transfer","shipment_id":sh.shipment_id,"vehicle_id":v.vehicle_id,
                                          "message":f"CARGO TRANSFER {sh.shipment_id}: {v.vehicle_id} will collect at {sh.handoff_city} and continue to {sh.delivery_city}"})
                        self._schedule_route(v)

    async def _advance_vehicles(self,sim_seconds):
        for v in self.vehicles.values():
            if v.status!="enroute" or not v.geometry:continue

            if v.service_remaining_min>0:
                v.service_remaining_min=max(0.0,v.service_remaining_min-sim_seconds/60.0)
                if v.service_remaining_min>0:continue

            old_progress=v.progress_km
            step_km=v.speed_kmh*sim_seconds/3600*max(0.25,self.traffic_factor)
            v.progress_km=min(v.route_total_km,v.progress_km+step_km)
            traveled=max(0.0,v.progress_km-old_progress)
            v.total_distance_km+=traveled;self.metrics["km"]+=traveled;self.metrics["cost"]+=traveled*v.cost_per_km
            if v.current_load_kg<=1e-6:
                v.empty_distance_km+=traveled;self.metrics["empty_km"]+=traveled
            if v.mission=="reposition":self.metrics["reposition_km"]+=traveled

            idx=0
            while idx<len(v.geometry_cum_km)-1 and v.geometry_cum_km[idx+1]<v.progress_km:idx+=1
            if idx<len(v.geometry)-1:
                a,b=v.geometry[idx],v.geometry[idx+1];span=v.geometry_cum_km[idx+1]-v.geometry_cum_km[idx]
                frac=0 if span<=0 else (v.progress_km-v.geometry_cum_km[idx])/span
                v.current_lat=a[0]+frac*(b[0]-a[0]);v.current_lon=a[1]+frac*(b[1]-a[1]);v.current_city=nearest_city(v.current_lat,v.current_lon)
            elif v.geometry:
                v.current_lat,v.current_lon=v.geometry[-1]

            changed=False
            for s in v.stops:
                if s.completed or s.cumulative_km>v.progress_km+1e-6:continue
                if not s.service_started:
                    s.service_started=True;v.service_remaining_min=max(0.0,s.service_minutes)
                    if v.service_remaining_min>0:continue
                s.completed=True;changed=True
                if s.kind=="reposition":
                    v.current_city=s.city;v.reposition_target=s.city
                    await self._emit({"type":"reposition","vehicle_id":v.vehicle_id,"decision":"arrived",
                                      "message":f"REPOSITION COMPLETE {v.vehicle_id} arrived at demand warehouse {s.city}"})
                    continue
                for sid in s.shipment_ids:
                    sh=self.shipments.get(sid)
                    if not sh:continue
                    if s.kind in ("pickup","handoff") and sh.status=="assigned":
                        sh.status="picked_up";sh.handoff_city=None
                        v.reserved_load_kg=max(0,v.reserved_load_kg-sh.weight_kg);v.reserved_volume_m3=max(0,v.reserved_volume_m3-sh.volume_m3)
                        v.current_load_kg+=sh.weight_kg;v.current_volume_m3+=sh.volume_m3
                    elif s.kind=="delivery" and sh.status in ("picked_up","assigned"):
                        sh.status="delivered";self.metrics["delivered"]+=1
                        v.current_load_kg=max(0,v.current_load_kg-sh.weight_kg);v.current_volume_m3=max(0,v.current_volume_m3-sh.volume_m3)
                        v.assigned_shipments=[x for x in v.assigned_shipments if x!=sid]
                        sh.adaptive_time_min=round((self.sim_time-(sh.order_arrival_at or sh.assigned_at or self.sim_time)).total_seconds()/60,2)
                        sh.time_saving_min=round(sh.baseline_time_min-sh.adaptive_time_min,2);self.metrics["time_saved_min"]+=sh.time_saving_min
                        if self.sim_time<=sh.delivery_deadline:self.metrics["on_time_deliveries"]+=1
                        await self._emit({"type":"delivery","shipment_id":sid,"vehicle_id":v.vehicle_id,
                                          "message":f"DELIVERED {sid} via {v.vehicle_id}: time saving {sh.time_saving_min:+.1f} min, cost saving USD {sh.cost_saving_usd:+,.0f}"})

            if changed:
                v.stops=[s for s in v.stops if not s.completed]

            if v.progress_km>=v.route_total_km-1e-6 and not v.stops and v.service_remaining_min<=0:
                v.status="idle";v.progress_km=0;v.geometry=[];v.geometry_cum_km=[];v.route_total_km=0;v.route_quality="none";v.service_remaining_min=0
                if v.mission=="reposition":v.current_city=v.reposition_target or v.current_city
                v.mission="freight";v.reposition_target=None

    def _schedule_route(self,v):
        old=self.route_tasks_by_vehicle.get(v.vehicle_id)
        if old and not old.done():old.cancel()
        v.route_version+=1;token=v.route_version
        async def task():
            points=[(v.current_lat,v.current_lon)]+[coords(s.city) for s in v.stops if not s.completed]
            if len(points)<2:return
            t=time.perf_counter();result=await self.router.route(points);ms=(time.perf_counter()-t)*1000
            async with self.lock:
                if token!=v.route_version or v.status=="broken":return
                v.geometry=result["geometry"];v.route_total_km=float(result["distance_km"]);v.geometry_cum_km=[];cum=0.0
                for i,p in enumerate(v.geometry):
                    if i:
                        from .geo import haversine_km
                        cum+=haversine_km(v.geometry[i-1],p)
                    v.geometry_cum_km.append(cum)
                v.progress_km=0.0;v.route_quality=result["quality"];v.last_route_ms=ms
                legcum=0.0
                for idx,s in enumerate([x for x in v.stops if not x.completed]):
                    if idx<len(result.get("legs_km",[])):legcum+=result["legs_km"][idx]
                    s.cumulative_km=legcum
            await self._emit({"type":"route","vehicle_id":v.vehicle_id,"quality":result["quality"],"route_ms":ms,"distance_km":result["distance_km"]})
        t=asyncio.create_task(task());self.route_tasks.add(t);self.route_tasks_by_vehicle[v.vehicle_id]=t;t.add_done_callback(self.route_tasks.discard)

    async def _maybe_global_reposition(self):
        if (self.sim_time-self.last_global_opt_sim).total_seconds()/60<self.GLOBAL_REOPT_MIN:return
        self.last_global_opt_sim=self.sim_time
        forecasts=[]
        for wh in self.cities[self.cities["type"]=="hub"]["city_id"].tolist():
            fc=self.forecaster.forecast(wh,self.sim_time,60)
            if fc.orders>0:
                forecasts.append({"warehouse":wh,"forecast_orders":fc.orders,"forecast_weight_kg":fc.weight_kg,"forecast_volume_m3":fc.volume_m3,"confidence":fc.confidence})
        plan=self.global_optimizer.optimize(self.vehicles.values(),forecasts,city_distance,max_repositions=3)
        self.reposition_plan=plan
        for item in plan:
            v=self.vehicles.get(item["vehicle_id"])
            if not v or v.status!="idle":continue
            if item["warehouse"]==v.current_city:continue
            v.status="enroute";v.mission="reposition";v.reposition_target=item["warehouse"]
            v.stops=[Stop(item["warehouse"],"reposition",[],service_minutes=0.0)]
            await self._emit({"type":"reposition","vehicle_id":v.vehicle_id,"warehouse":item["warehouse"],
                              "message":f"REPOSITION {v.vehicle_id} → {item['warehouse']} for forecast demand {item['forecast_orders']:.1f} orders/hour"})
            self._schedule_route(v)

    async def submit_live_order(self,order):
        await self.live_queue.put(order)
        await self._emit({"type":"ingest","shipment_id":order.get("shipment_id"),"message":f"LIVE ORDER INGESTED {order.get('shipment_id')}"})

    async def set_traffic(self,factor):
        self.traffic_factor=factor
        await self._emit({"type":"network","traffic_factor":factor,"message":f"NETWORK TRAFFIC FACTOR {factor:.2f}x"})

    async def trigger_breakdown(self,vehicle_id):
        v=self.vehicles.get(vehicle_id)
        if not v:return False
        v.status="broken";v.route_version+=1;self.metrics["breakdowns"]+=1
        affected=set(v.assigned_shipments)
        breakdown_city=v.current_city
        for sid in affected:
            sh=self.shipments.get(sid)
            if not sh or sh.status=="delivered":continue
            sh.status="queued";sh.assigned_vehicle=None;sh.queued_since=self.sim_time;sh.handoff_city=breakdown_city
            self._queue_order(sh)
        v.assigned_shipments=[];v.current_load_kg=0;v.reserved_load_kg=0;v.current_volume_m3=0;v.reserved_volume_m3=0;v.stops=[];v.geometry=[]
        await self._emit({"type":"disruption","kind":"breakdown","vehicle_id":vehicle_id,
                          "message":f"BREAKDOWN {vehicle_id} at {breakdown_city}; {len(affected)} shipment(s) eligible for emergency cargo transfer"})
        return True

    async def recover_vehicle(self,vehicle_id):
        v=self.vehicles.get(vehicle_id)
        if not v:return False
        v.status="idle";v.mission="freight";v.reposition_target=None
        await self._emit({"type":"disruption","kind":"repair","vehicle_id":vehicle_id,"message":f"REPAIRED {vehicle_id}"})
        return True

    async def pause(self):
        self.running=False
        await self._emit({"type":"engine","action":"paused"})

    async def resume(self):
        await self.start()
        await self._emit({"type":"engine","action":"resumed"})

    def shipment_comparison(self,shipment_id):
        sh=self.shipments.get(shipment_id)
        if not sh:return None
        return {
            "shipment_id":sh.shipment_id,"status":sh.status,"warehouse":sh.warehouse_city,"delivery":sh.delivery_city,
            "weight_kg":sh.weight_kg,"volume_m3":sh.volume_m3,"priority":sh.priority,
            "traditional":{"dedicated_truck":True,"distance_km":sh.baseline_distance_km,"cost_usd":sh.baseline_cost_usd,"time_min":sh.baseline_time_min},
            "adaptive":{"vehicle_id":sh.assigned_vehicle,"distance_km":sh.adaptive_distance_km,"cost_usd":sh.actual_cost_usd,"time_min":sh.adaptive_time_min,
                        "cost_saving_usd":sh.cost_saving_usd,"time_saving_min":sh.time_saving_min,"backhaul":sh.is_backhaul,
                        "decision_reason":sh.decision_reason},
            "driver_offer":[o for o in self.driver_offers.values() if o["shipment_id"]==shipment_id][-1:]}

    def snapshot(self):
        now=self.sim_time
        assigned=[sh for sh in self.shipments.values() if sh.status!="queued"]
        baseline_assigned=sum(sh.baseline_cost_usd for sh in assigned)
        adaptive_cost=self.metrics["allocated_actual_cost"]
        cost_savings=baseline_assigned-adaptive_cost
        delivered=self.metrics["delivered"]
        avg_time_saved=self.metrics["time_saved_min"]/max(1,delivered)
        warehouse_rows=[]
        incoming_by_wh={}

        for wh in self.cities[self.cities["type"]=="hub"]["city_id"].tolist():
            ids=self.warehouse_queues.get(wh,[])
            live=[self.shipments[sid] for sid in ids if sid in self.shipments and self.shipments[sid].status=="queued"]
            oldest=min((s.queued_since or now) for s in live) if live else None
            wait=(now-oldest).total_seconds()/60 if oldest else 0.0
            available=[v.vehicle_id for v in self.vehicles.values() if v.status=="idle" and v.current_city==wh]
            candidates=[]
            for v in self.vehicles.values():
                if v.status!="enroute" or v.mission!="freight" or v.remaining_capacity_kg<=0:continue
                dist=city_distance(v.current_city,wh);eta=dist/max(1.0,v.speed_kmh)*60
                if eta<=180:
                    candidates.append({"vehicle_id":v.vehicle_id,"eta_min":round(eta,1),"remaining_kg":round(v.remaining_capacity_kg,1),
                                       "remaining_volume_m3":round(v.remaining_volume_m3,2),"current_city":v.current_city})
            candidates=sorted(candidates,key=lambda x:x["eta_min"])[:5]
            incoming_by_wh[wh]=candidates
            fc=self.forecaster.forecast(wh,now,60)
            departure_deadline=(oldest+timedelta(minutes=self.WAREHOUSE_DEPARTURE_MIN)).isoformat() if oldest else None
            warehouse_rows.append({
                "warehouse":wh,"orders":len(live),"queue_depth":len(live),
                "weight_kg":round(sum(s.weight_kg for s in live),1),"volume_m3":round(sum(s.volume_m3 for s in live),2),
                "oldest_wait_min":round(wait,1),"departure_due":bool(live and wait>=self.WAREHOUSE_DEPARTURE_MIN),
                "departure_deadline":departure_deadline,"departure_in_min":round(max(0,self.WAREHOUSE_DEPARTURE_MIN-wait),1) if live else None,
                "available_vehicle_count":len(available),"available_vehicles":available,
                "incoming_trucks":candidates,"forecast_orders_next_hour":fc.orders,
                "forecast_weight_kg_next_hour":fc.weight_kg,"forecast_volume_m3_next_hour":fc.volume_m3,
                "forecast_confidence":fc.confidence
            })

        recent_shipments=[self.shipment_comparison(sh.shipment_id) for sh in list(self.shipments.values())[-25:]]
        pending_offers=[o for o in self.driver_offers.values() if o["status"]=="pending"]

        return {
            "sim_time":self.sim_time.isoformat(),"running":self.running,"speed":self.speed,
            "orders_processed":self.metrics["received"],"orders_total":len(self.orders),
            "queue_depth":sum(1 for s in self.shipments.values() if s.status=="queued"),
            "metrics":{
                **self.metrics,
                "consolidation_rate":round(100*self.metrics["consolidated"]/max(1,self.metrics["received"]),1),
                "fleet_utilization":round(sum(v.utilization_pct for v in self.vehicles.values())/max(1,len(self.vehicles)),1),
                "at_risk":sum(1 for sh in self.shipments.values() if sh.status in ("assigned","picked_up") and sh.delivery_deadline<now+timedelta(minutes=30)),
                "optimizer_p95_ms":self.optimizer.p95_ms(),
                "route_requests":self.router.requests,"real_routes":self.router.real_routes,"fallback_routes":self.router.fallback_routes,
                "baseline_assigned_cost":round(baseline_assigned,2),"cost_savings_usd":round(cost_savings,2),
                "cost_savings_pct":round(100*cost_savings/max(1,baseline_assigned),1),
                "avg_time_saved_min":round(avg_time_saved,1),
                "dispatches_avoided":max(0,self.metrics["processed"]-self.metrics["dispatched"]),
                "empty_km_pct":round(100*self.metrics["empty_km"]/max(1,self.metrics["km"]),1),
                "on_time_pct":round(100*self.metrics["on_time_deliveries"]/max(1,self.metrics["delivered"]),1)
            },
            "vehicles":[v.snapshot() for v in self.vehicles.values()],
            "warehouses":warehouse_rows,
            "driver_offers":pending_offers,
            "reposition_plan":self.reposition_plan,
            "shipments":recent_shipments,
            "history":self.history[-100:],
            "router":{"url":self.router.base_url,"last_error":self.router.last_error,
                      "real_ratio":round(100*self.router.real_routes/max(1,self.router.real_routes+self.router.fallback_routes),1),
                      "circuit_open":self.router.circuit_open,
                      "retry_in_s":max(0,round(self.router.open_until-time.time(),1)) if self.router.circuit_open else 0},
            "global_optimizer":{"runs":self.global_optimizer.runs,"status":self.global_optimizer.last_status},
            "live_stream":{"enabled":self._live_stream_enabled,"interval_s":self._live_stream_interval}
        }
