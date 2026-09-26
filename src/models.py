from __future__ import annotations
from dataclasses import dataclass,field,asdict
from datetime import datetime
from typing import Literal,Optional

ShipmentStatus=Literal["pending","assigned","picked_up","delivered","queued","cancelled"]
StopKind=Literal["pickup","delivery","handoff","reposition"]

@dataclass
class Shipment:
    shipment_id:str
    pickup_city:str
    delivery_city:str
    weight_kg:float
    volume_m3:float
    revenue_usd:float
    pickup_deadline:datetime
    delivery_deadline:datetime
    priority:str
    status:ShipmentStatus="pending"
    assigned_vehicle:Optional[str]=None
    assigned_at:Optional[datetime]=None
    warehouse_city:Optional[str]=None
    original_warehouse_city:Optional[str]=None
    handoff_city:Optional[str]=None
    package_length_m:float=0.0
    package_width_m:float=0.0
    package_height_m:float=0.0
    pickup_service_min:float=15.0
    delivery_service_min:float=12.0
    baseline_distance_km:float=0.0
    baseline_cost_usd:float=0.0
    baseline_time_min:float=0.0
    baseline_eta:Optional[datetime]=None
    adaptive_distance_km:float=0.0
    adaptive_time_min:float=0.0
    actual_cost_usd:float=0.0
    cost_saving_usd:float=0.0
    time_saving_min:float=0.0
    queued_since:Optional[datetime]=None
    order_arrival_at:Optional[datetime]=None
    is_backhaul:bool=False
    decision_reason:str=""
    decision_score:float=0.0

    def to_dict(self):
        d=asdict(self)
        for k in ("pickup_deadline","delivery_deadline","assigned_at","baseline_eta","queued_since","order_arrival_at"):
            if d[k]: d[k]=d[k].isoformat()
        return d

    @classmethod
    def from_dict(cls,d:dict)->"Shipment":
        d=dict(d)
        for k in ("pickup_deadline","delivery_deadline","assigned_at","baseline_eta","queued_since"):
            if d.get(k): d[k]=datetime.fromisoformat(d[k])
        return cls(**d)

@dataclass
class Stop:
    city:str
    kind:StopKind
    shipment_ids:list[str]
    completed:bool=False
    cumulative_km:float=0.0
    service_minutes:float=0.0
    service_started:bool=False

@dataclass
class Vehicle:
    vehicle_id:str
    vehicle_type:str
    capacity_kg:float
    max_distance_km:float
    cost_per_km:float
    speed_kmh:float
    base_city:str
    current_lat:float=0.0
    current_lon:float=0.0
    current_city:str=""
    status:str="idle"
    mission:str="freight"
    reposition_target:Optional[str]=None
    service_remaining_min:float=0.0
    current_load_kg:float=0.0
    reserved_load_kg:float=0.0
    current_volume_m3:float=0.0
    reserved_volume_m3:float=0.0
    volume_capacity_m3:float=0.0
    assigned_shipments:list[str]=field(default_factory=list)
    stops:list[Stop]=field(default_factory=list)
    geometry:list[tuple[float,float]]=field(default_factory=list)
    geometry_cum_km:list[float]=field(default_factory=list)
    route_total_km:float=0.0
    progress_km:float=0.0
    route_quality:str="none"
    total_distance_km:float=0.0
    empty_distance_km:float=0.0
    route_version:int=0
    last_route_ms:float=0.0

    @property
    def remaining_capacity_kg(self):
        return max(0.0,self.capacity_kg-self.current_load_kg-self.reserved_load_kg)

    @property
    def remaining_volume_m3(self):
        if self.volume_capacity_m3<=0:return float("inf")
        return max(0.0,self.volume_capacity_m3-self.current_volume_m3-self.reserved_volume_m3)

    @property
    def utilization_pct(self):
        kg_pct=100.0*(self.current_load_kg+self.reserved_load_kg)/self.capacity_kg if self.capacity_kg else 0.0
        vol_pct=100.0*(self.current_volume_m3+self.reserved_volume_m3)/self.volume_capacity_m3 if self.volume_capacity_m3 else 0.0
        return min(100.0,max(kg_pct,vol_pct))

    def snapshot(self):
        return {
            "vehicle_id":self.vehicle_id,"type":self.vehicle_type,"status":self.status,"mission":self.mission,
            "reposition_target":self.reposition_target,"current_city":self.current_city,
            "lat":self.current_lat,"lon":self.current_lon,"load_kg":round(self.current_load_kg,1),
            "reserved_load_kg":round(self.reserved_load_kg,1),"capacity_kg":self.capacity_kg,
            "volume_m3":round(self.current_volume_m3+self.reserved_volume_m3,2),"volume_capacity_m3":self.volume_capacity_m3,
            "remaining_capacity_kg":round(self.remaining_capacity_kg,1),
            "remaining_volume_m3":round(self.remaining_volume_m3,2) if self.remaining_volume_m3!=float("inf") else None,
            "utilization_pct":round(self.utilization_pct,1),"shipments":list(self.assigned_shipments),
            "route_stops":[{"city":s.city,"kind":s.kind,"shipment_ids":s.shipment_ids,"completed":s.completed} for s in self.stops if not s.completed],
            "geometry":self.geometry,"route_quality":self.route_quality,"route_total_km":round(self.route_total_km,2),
            "progress_km":round(self.progress_km,2),"distance_km":round(self.total_distance_km,2),
            "empty_distance_km":round(self.empty_distance_km,2),"last_route_ms":round(self.last_route_ms,2),
            "route_version":self.route_version
        }

def vehicle_from_dict(d:dict)->"Vehicle":
    d=dict(d)
    d["stops"]=[Stop(**s) for s in d.get("stops",[])]
    d["geometry"]=[tuple(p) for p in d.get("geometry",[])]
    d["geometry_cum_km"]=list(d.get("geometry_cum_km",[]))
    return Vehicle(**d)
