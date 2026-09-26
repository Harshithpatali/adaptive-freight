from __future__ import annotations
import asyncio,time
from datetime import datetime
from .geo import coords,city_distance,route_distance
from .models import Vehicle,Stop,Shipment

class Optimizer:
    def __init__(self,router):
        self.router=router
        self.decisions=0
        self.latencies=[]
        self.last_rejections=[]

    def _capacity_ok(self,v,sh):
        return v.remaining_capacity_kg>=sh.weight_kg-1e-6 and v.remaining_volume_m3>=sh.volume_m3-1e-6

    def _local_candidate(self,v,sh,now,road_hint=None):
        if v.status!="enroute" or not self._capacity_ok(v,sh):return []
        pickup=getattr(sh,"handoff_city",None) or sh.warehouse_city or sh.pickup_city
        base=[s for s in v.stops if not s.completed]
        results=[]
        old_dist=route_distance([v.current_city]+[s.city for s in base]) if base else 0.0
        old_service=sum(s.service_minutes for s in base)
        for pidx in range(len(base)+1):
            pstop=Stop(pickup,"pickup",[sh.shipment_id],service_minutes=sh.pickup_service_min)
            s1=base[:pidx]+[pstop]+base[pidx:]
            for didx in range(pidx+1,len(s1)+1):
                dstop=Stop(sh.delivery_city,"delivery",[sh.shipment_id],service_minutes=sh.delivery_service_min)
                candidate=s1[:didx]+[dstop]+s1[didx:]
                cities=[v.current_city]+[s.city for s in candidate]
                new_dist=route_distance(cities)
                extra=max(0.0,new_dist-old_dist)
                detour=(extra/old_dist) if old_dist>50 else 0.0
                if new_dist>v.max_distance_km:
                    continue
                if old_dist>50 and detour>0.55:
                    continue
                local_time=(new_dist/max(1.0,v.speed_kmh))*60.0+sum(s.service_minutes for s in candidate)
                road_time=None
                if road_hint:
                    road_time=road_hint["to_pickup_min"]+sh.pickup_service_min+road_hint["pickup_to_delivery_min"]+sh.delivery_service_min
                    eta_min=max(local_time,road_time)
                else:
                    eta_min=local_time

                pickup_eta=now.timestamp()+0
                pickup_min=(road_hint["to_pickup_min"] if road_hint else city_distance(v.current_city,pickup)/max(1.0,v.speed_kmh)*60)+sh.pickup_service_min
                delivery_min=eta_min
                pickup_deadline_ok=(now.timestamp()+pickup_min*60)<=sh.pickup_deadline.timestamp()
                delivery_deadline_ok=(now.timestamp()+delivery_min*60)<=sh.delivery_deadline.timestamp()
                if not pickup_deadline_ok or not delivery_deadline_ok:
                    continue

                incremental_cost=extra*v.cost_per_km
                savings=sh.baseline_cost_usd-incremental_cost
                sla_buffer=max(0.0,(sh.delivery_deadline.timestamp()-now.timestamp())/60-delivery_min)
                backhaul=city_distance(pickup,v.base_city)>city_distance(sh.delivery_city,v.base_city)
                score=1000+savings*0.8-extra*1.2-detour*240+sla_buffer*0.8
                if sh.priority=="Express":score+=40
                if backhaul:score+=25
                results.append((score,candidate,extra,detour,eta_min,pickup_min,savings,sla_buffer,backhaul,
                                f"Capacity OK; road ETA {eta_min:.0f} min; SLA buffer {sla_buffer:.0f} min; detour {detour*100:.1f}%"))
        return sorted(results,key=lambda x:x[0],reverse=True)[:5]

    def evaluate(self,vehicles:list[Vehicle],shipment:Shipment,now:datetime|None=None):
        t0=time.perf_counter()
        now=now or datetime.now()
        eligible=[v for v in vehicles if self._capacity_ok(v,shipment)]
        self.last_rejections=[]
        scored=[]
        for v in sorted(eligible,key=lambda v:city_distance(v.current_city,shipment.warehouse_city or shipment.pickup_city))[:40]:
            c=self._local_candidate(v,shipment,now)
            if c:scored.append((c[0][0],v,c[0]))
            elif v.status=="enroute":
                self.last_rejections.append({"vehicle_id":v.vehicle_id,"reason":"capacity/route/SLA constraints"})
        if not scored:
            ms=(time.perf_counter()-t0)*1000;self.latencies.append(ms);return None,{"decision_ms":ms,"rejections":self.last_rejections[:8]}
        best=max(scored,key=lambda x:x[0]);ms=(time.perf_counter()-t0)*1000;self.latencies.append(ms);self.decisions+=1
        c=best[2]
        return {"vehicle":best[1],"stops":c[1],"extra_km":c[2],"detour_pct":c[3]*100,"score":c[0],"eta_min":c[4],
                "pickup_eta_min":c[5],"savings_usd":c[6],"sla_buffer_min":c[7],"backhaul":c[8],"reason":c[9],
                "rejections":self.last_rejections[:8]},{"decision_ms":ms,"rejections":self.last_rejections[:8]}

    async def evaluate_async(self,vehicles:list[Vehicle],shipment:Shipment,now:datetime):
        t0=time.perf_counter()
        eligible=[v for v in vehicles if v.status=="enroute" and self._capacity_ok(v,shipment)]
        self.last_rejections=[]
        if not eligible:
            ms=(time.perf_counter()-t0)*1000;self.latencies.append(ms);return None,{"decision_ms":ms,"rejections":[]}
        ranked=sorted(eligible,key=lambda v:city_distance(v.current_city,shipment.warehouse_city or shipment.pickup_city))[:20]
        pickup=shipment.warehouse_city or shipment.pickup_city
        try:
            hints=await self.router.table_current_to_jobs([coords(v.current_city) for v in ranked],coords(pickup),coords(shipment.delivery_city))
        except Exception:
            hints=[None]*len(ranked)
        scored=[]
        for v,hint in zip(ranked,hints):
            candidates=self._local_candidate(v,shipment,now,hint)
            if candidates:scored.append((candidates[0][0],v,candidates[0]))
            else:self.last_rejections.append({"vehicle_id":v.vehicle_id,"reason":"OSRM/SLA/detour/capacity constraint"})
        if not scored:
            ms=(time.perf_counter()-t0)*1000;self.latencies.append(ms);return None,{"decision_ms":ms,"rejections":self.last_rejections[:8]}
        best=max(scored,key=lambda x:x[0]);ms=(time.perf_counter()-t0)*1000;self.latencies.append(ms);self.decisions+=1
        c=best[2]
        return {"vehicle":best[1],"stops":c[1],"extra_km":c[2],"detour_pct":c[3]*100,"score":c[0],"eta_min":c[4],
                "pickup_eta_min":c[5],"savings_usd":c[6],"sla_buffer_min":c[7],"backhaul":c[8],"reason":c[9],
                "rejections":self.last_rejections[:8],"travel_source":"OSRM Table API"},{"decision_ms":ms,"rejections":self.last_rejections[:8]}

    def p95_ms(self):
        if not self.latencies:return 0.0
        s=sorted(self.latencies)
        return round(s[min(len(s)-1,int(.95*len(s)))],2)
