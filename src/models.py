from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal, Optional

ShipmentStatus = Literal["pending", "assigned", "picked_up", "delivered", "queued", "cancelled"]
StopKind = Literal["pickup", "delivery"]

@dataclass
class Shipment:
    shipment_id: str
    pickup_city: str
    delivery_city: str
    weight_kg: float
    volume_m3: float
    revenue_usd: float
    pickup_deadline: datetime
    delivery_deadline: datetime
    priority: str
    status: ShipmentStatus = "pending"
    assigned_vehicle: Optional[str] = None
    assigned_at: Optional[datetime] = None

    def to_dict(self):
        d=asdict(self)
        for k in ("pickup_deadline","delivery_deadline","assigned_at"):
            if d[k]: d[k]=d[k].isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Shipment":
        d=dict(d)
        for k in ("pickup_deadline","delivery_deadline","assigned_at"):
            if d.get(k): d[k]=datetime.fromisoformat(d[k])
        return cls(**d)

@dataclass
class Stop:
    city: str
    kind: StopKind
    shipment_ids: list[str]
    completed: bool=False
    cumulative_km: float=0.0

@dataclass
class Vehicle:
    vehicle_id: str
    vehicle_type: str
    capacity_kg: float
    max_distance_km: float
    cost_per_km: float
    speed_kmh: float
    base_city: str
    current_lat: float=0.0
    current_lon: float=0.0
    current_city: str=""
    status: str="idle"
    current_load_kg: float=0.0
    reserved_load_kg: float=0.0
    assigned_shipments: list[str]=field(default_factory=list)
    stops: list[Stop]=field(default_factory=list)
    geometry: list[tuple[float,float]]=field(default_factory=list)
    geometry_cum_km: list[float]=field(default_factory=list)
    route_total_km: float=0.0
    progress_km: float=0.0
    route_quality: str="none"
    total_distance_km: float=0.0
    route_version: int=0
    last_route_ms: float=0.0

    @property
    def remaining_capacity_kg(self):
        return max(0.0, self.capacity_kg - self.current_load_kg - self.reserved_load_kg)

    @property
    def utilization_pct(self):
        return min(100.0, 100.0*(self.current_load_kg+self.reserved_load_kg)/self.capacity_kg) if self.capacity_kg else 0.0

    def snapshot(self):
        return {
            "vehicle_id": self.vehicle_id,
            "type": self.vehicle_type,
            "status": self.status,
            "current_city": self.current_city,
            "lat": self.current_lat,
            "lon": self.current_lon,
            "load_kg": round(self.current_load_kg,1),
            "reserved_load_kg": round(self.reserved_load_kg,1),
            "capacity_kg": self.capacity_kg,
            "remaining_capacity_kg": round(self.remaining_capacity_kg,1),
            "utilization_pct": round(self.utilization_pct,1),
            "shipments": list(self.assigned_shipments),
            "route_stops": [
                {"city":s.city,"kind":s.kind,"shipment_ids":s.shipment_ids,"completed":s.completed}
                for s in self.stops if not s.completed
            ],
            "geometry": self.geometry,
            "route_quality": self.route_quality,
            "route_total_km": round(self.route_total_km,2),
            "progress_km": round(self.progress_km,2),
            "distance_km": round(self.total_distance_km,2),
            "last_route_ms": round(self.last_route_ms,2),
            "route_version": self.route_version,
        }

def vehicle_from_dict(d: dict) -> "Vehicle":
    d=dict(d)
    d["stops"]=[Stop(**s) for s in d.get("stops",[])]
    d["geometry"]=[tuple(p) for p in d.get("geometry",[])]
    d["geometry_cum_km"]=list(d.get("geometry_cum_km",[]))
    return Vehicle(**d)
