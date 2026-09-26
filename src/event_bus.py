from __future__ import annotations
import asyncio,json,time
try:
    import redis.asyncio as redis
except Exception:
    redis=None

class EventBus:
    def __init__(self,url:str=""):
        self.url=url; self.redis=None; self.subscribers=set(); self.events=[]; self.max_events=250
    async def start(self):
        if self.url and redis:
            try:
                self.redis=redis.from_url(self.url,decode_responses=True); await self.redis.ping()
            except Exception:
                self.redis=None
    async def close(self):
        if self.redis: await self.redis.aclose()
    async def publish(self,event:dict):
        event={**event,"published_at":time.time()}
        self.events.append(event); self.events=self.events[-self.max_events:]
        payload=json.dumps(event,default=str)
        for q in list(self.subscribers):
            try: q.put_nowait(event)
            except asyncio.QueueFull: pass
        if self.redis:
            try: await self.redis.xadd("adaptive:events",{"payload":payload},maxlen=10000,approximate=True)
            except Exception: pass
    async def subscribe(self):
        q=asyncio.Queue(maxsize=5); self.subscribers.add(q)
        try:
            while True: yield await q.get()
        finally: self.subscribers.discard(q)
