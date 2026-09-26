from __future__ import annotations
import asyncio, time
from collections import OrderedDict
import httpx
from .config import settings
from .geo import coords, haversine_km

class RoadRouter:
    def __init__(self):
        self.base_url=settings.router_url; self.timeout=settings.route_timeout_seconds
        self.cache=OrderedDict(); self.max_cache=settings.route_cache_size
        self.client: httpx.AsyncClient|None=None; self.last_error=""; self.requests=0
        self.real_routes=0; self.fallback_routes=0; self.semaphore=asyncio.Semaphore(8); self.inflight=0
        self.consecutive_failures=0; self.failure_threshold=3; self.open_until=0.0; self.cooldown_seconds=15.0
    async def start(self):
        if self.client is None: self.client=httpx.AsyncClient(timeout=httpx.Timeout(self.timeout,connect=0.8))
    async def close(self):
        if self.client: await self.client.aclose()
    @property
    def circuit_open(self): return time.time() < self.open_until
    def _record_success(self): self.consecutive_failures=0; self.open_until=0.0
    def _record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold: self.open_until=time.time()+self.cooldown_seconds
    def _key(self,pts): return tuple((round(lat,5),round(lon,5)) for lat,lon in pts)
    def _put(self,key,val):
        self.cache[key]=val; self.cache.move_to_end(key)
        while len(self.cache)>self.max_cache: self.cache.popitem(last=False)
    async def route(self,points:list[tuple[float,float]]):
        if len(points)<2: return {"geometry":points,"distance_km":0.0,"duration_min":0.0,"quality":"fallback","legs_km":[]}
        key=self._key(points)
        if key in self.cache:
            self.cache.move_to_end(key); return self.cache[key]
        if self.circuit_open:
            val=self._fallback(points); self._put(key,val); self.fallback_routes+=1; return val
        self.requests+=1; await self.start()
        coord=";".join(f"{lon},{lat}" for lat,lon in points); url=f"{self.base_url}/route/v1/driving/{coord}"; t=time.perf_counter()
        try:
            async with self.semaphore:
                self.inflight+=1
                try: r=await self.client.get(url,params={"overview":"full","geometries":"geojson","steps":"false"})
                finally: self.inflight=max(0,self.inflight-1)
            r.raise_for_status(); data=r.json(); rt=data["routes"][0]
            geom=[(p[1],p[0]) for p in rt["geometry"]["coordinates"]]
            legs=[leg["distance"]/1000 for leg in rt.get("legs",[])]
            val={"geometry":geom,"distance_km":rt["distance"]/1000,"duration_min":rt["duration"]/60,"quality":"real","legs_km":legs,"latency_ms":(time.perf_counter()-t)*1000}
            self._put(key,val); self.real_routes+=1; self.last_error=""; self._record_success(); return val
        except Exception as e:
            self.last_error=str(e); self._record_failure(); val=self._fallback(points); self._put(key,val); self.fallback_routes+=1; return val
    def _fallback(self,points):
        d=sum(haversine_km(a,b)*1.11 for a,b in zip(points,points[1:]))
        return {"geometry":points,"distance_km":d,"duration_min":d/65*60,"quality":"fallback","legs_km":[]}
    async def table_current_to_jobs(self,current_points:list[tuple[float,float]],pickup:tuple[float,float],delivery:tuple[float,float]):
        if not current_points: return []
        n=len(current_points)
        if self.circuit_open:
            return [{"to_pickup_km":haversine_km(p,pickup)*1.11,"pickup_to_delivery_km":haversine_km(pickup,delivery)*1.11,
                     "to_pickup_min":haversine_km(p,pickup)*1.11/65*60,"pickup_to_delivery_min":haversine_km(pickup,delivery)*1.11/65*60} for p in current_points]
        pts=current_points+[pickup,delivery]; coord=";".join(f"{lon},{lat}" for lat,lon in pts); await self.start()
        try:
            r=await self.client.get(f"{self.base_url}/table/v1/driving/{coord}",params={"annotations":"distance,duration"}); r.raise_for_status(); data=r.json(); self._record_success()
            return [{"to_pickup_km":data["distances"][i][n]/1000,"pickup_to_delivery_km":data["distances"][n][n+1]/1000,
                     "to_pickup_min":data["durations"][i][n]/60,"pickup_to_delivery_min":data["durations"][n][n+1]/60} for i in range(n)]
        except Exception as e:
            self.last_error=str(e); self._record_failure()
            return [{"to_pickup_km":haversine_km(p,pickup)*1.11,"pickup_to_delivery_km":haversine_km(pickup,delivery)*1.11,
                     "to_pickup_min":haversine_km(p,pickup)*1.11/65*60,"pickup_to_delivery_min":haversine_km(pickup,delivery)*1.11/65*60} for p in current_points]
