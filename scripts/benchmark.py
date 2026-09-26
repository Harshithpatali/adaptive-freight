import asyncio,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.engine import Engine
async def fake_route(points):
    return {"geometry":points,"distance_km":10*(len(points)-1),"duration_min":10,"quality":"real","legs_km":[10]*(len(points)-1)}
async def main():
    e=Engine(); e.router.route=fake_route; t=time.perf_counter()
    for i in range(1000): await e._process_order(e.orders.iloc[i].to_dict())
    dt=time.perf_counter()-t
    print({"orders":1000,"seconds":round(dt,3),"orders_per_sec":round(1000/dt,1),"optimizer_p95_ms":e.optimizer.p95_ms(),"consolidated":e.metrics["consolidated"],"dispatches":e.metrics["dispatched"]})
asyncio.run(main())
