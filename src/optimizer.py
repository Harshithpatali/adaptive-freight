from __future__ import annotations
import time
from .geo import city_distance, route_distance
from .models import Vehicle, Stop, Shipment

class Optimizer:
    def __init__(self,router):
        self.router=router; self.decisions=0; self.latencies=[]
    def evaluate(self,vehicles:list[Vehicle],shipment:Shipment):
        t0=time.perf_counter()
        eligible=[v for v in vehicles if v.status!="broken" and v.remaining_capacity_kg>=shipment.weight_kg-1e-6]
        if not eligible:
            ms=(time.perf_counter()-t0)*1000; self.latencies.append(ms); return None,{"decision_ms":ms}
        eligible.sort(key=lambda v:(0 if v.assigned_shipments else 1,city_distance(v.current_city,shipment.pickup_city)))
        scored=[]
        for v in eligible[:40]:
            candidates=self._candidate_insertions(v,shipment)
            if not candidates: continue
            score,candidate,extra,detour=candidates[0]; scored.append((score,v,candidate,extra,detour))
        if not scored:
            ms=(time.perf_counter()-t0)*1000; self.latencies.append(ms); return None,{"decision_ms":ms}
        best=max(scored,key=lambda x:x[0]); ms=(time.perf_counter()-t0)*1000; self.latencies.append(ms); self.decisions+=1
        return {"vehicle":best[1],"stops":best[2],"extra_km":best[3],"detour_pct":best[4]*100,"score":best[0]},{"decision_ms":ms}
    def _candidate_insertions(self,vehicle:Vehicle,shipment:Shipment):
        if vehicle.status=="broken" or vehicle.remaining_capacity_kg<shipment.weight_kg-1e-6:return []
        base=vehicle.stops[:]; results=[]
        for pidx in range(len(base)+1):
            s1=base[:pidx]+[Stop(shipment.pickup_city,"pickup",[shipment.shipment_id])]+base[pidx:]
            for didx in range(pidx+1,len(s1)+1):
                candidate=s1[:didx]+[Stop(shipment.delivery_city,"delivery",[shipment.shipment_id])]+s1[didx:]
                cities=[vehicle.current_city]+[s.city for s in candidate]
                old_dist=route_distance([vehicle.current_city]+[s.city for s in base]) if base else 0.0
                new_dist=route_distance(cities); extra=max(0,new_dist-old_dist); detour=(extra/old_dist) if old_dist>50 else 0.0
                if new_dist>vehicle.max_distance_km or (old_dist>50 and detour>0.55): continue
                score=1000-extra*1.25-detour*260
                if vehicle.assigned_shipments: score+=180
                if shipment.priority=="Express": score+=35
                results.append((score,candidate,extra,detour))
        return sorted(results,key=lambda x:x[0],reverse=True)[:3]
    def p95_ms(self):
        if not self.latencies:return 0.0
        s=sorted(self.latencies); return round(s[min(len(s)-1,int(.95*len(s)))],2)
