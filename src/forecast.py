from __future__ import annotations
from collections import defaultdict,deque
from dataclasses import dataclass
from datetime import datetime,timedelta

@dataclass
class DemandForecast:
    warehouse: str
    horizon_min: float
    orders: float
    weight_kg: float
    volume_m3: float
    confidence: float

class DemandForecaster:
    def __init__(self,window_min: float = 90.0, max_events: int = 500):
        self.window_min=window_min
        self.events=defaultdict(lambda: deque(maxlen=max_events))

    def observe(self, warehouse: str, ts: datetime, weight_kg: float, volume_m3: float):
        self.events[warehouse].append((ts, float(weight_kg), float(volume_m3)))

    def forecast(self, warehouse: str, now: datetime, horizon_min: float = 60.0) -> DemandForecast:
        rows=[r for r in self.events.get(warehouse,()) if (now-r[0]).total_seconds() <= self.window_min*60]
        if not rows:
            return DemandForecast(warehouse,horizon_min,0.0,0.0,0.0,0.0)
        span=max(15.0,min(self.window_min,(now-min(r[0] for r in rows)).total_seconds()/60 or 15.0))
        rate=len(rows)/span
        factor=horizon_min/60.0
        orders=rate*60.0*factor
        weight=sum(r[1] for r in rows)/max(1,len(rows))*orders
        volume=sum(r[2] for r in rows)/max(1,len(rows))*orders
        confidence=min(1.0, len(rows)/20.0) * min(1.0, span/60.0)
        return DemandForecast(warehouse,horizon_min,round(orders,1),round(weight,1),round(volume,2),round(confidence,2))
