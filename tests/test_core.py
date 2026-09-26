import asyncio
from datetime import datetime,timedelta
from src.models import Vehicle,Shipment,Stop
from src.optimizer import Optimizer
from src.forecast import DemandForecaster

class FakeRouter:
    async def table_current_to_jobs(self,current_points,pickup,delivery):
        return [{"to_pickup_km":20,"pickup_to_delivery_km":100,"to_pickup_min":20,"pickup_to_delivery_min":90} for _ in current_points]

def test_optimizer_prefers_active():
    v=Vehicle('V1','Box Truck',4500,1500,.8,60,'DAL',current_city='DAL')
    v.assigned_shipments=['OLD'];v.reserved_load_kg=1000;v.status='enroute';v.stops=[Stop('HOU','delivery',['OLD'])]
    s=Shipment('S1','DAL','HOU',1000,4,600,datetime.now()+timedelta(hours=2),datetime.now()+timedelta(hours=8),'Standard')
    d,stats=Optimizer(FakeRouter()).evaluate([v],s)
    assert d and d['vehicle'].vehicle_id=='V1'
    assert 'reason' in d
    assert d['sla_buffer_min']>0

def test_capacity_blocks():
    v=Vehicle('V1','Box Truck',4500,1500,.8,60,'DAL',current_city='DAL');v.reserved_load_kg=4400
    s=Shipment('S1','DAL','HOU',500,4,600,datetime.now()+timedelta(hours=1),datetime.now()+timedelta(hours=2),'Standard')
    d,_=Optimizer(FakeRouter()).evaluate([v],s)
    assert d is None

def test_osrm_time_feasibility():
    async def run():
        v=Vehicle('V1','Box Truck',4500,1500,.8,60,'DAL',current_city='DAL');v.status='enroute'
        now=datetime.now()
        s=Shipment('S1','DAL','HOU',500,2,600,now+timedelta(minutes=10),now+timedelta(minutes=20),'Express')
        d,_=await Optimizer(FakeRouter()).evaluate_async([v],s,now)
        assert d is None
    asyncio.run(run())

def test_demand_forecast():
    f=DemandForecaster()
    now=datetime(2026,1,1,12,0)
    for i in range(10):
        f.observe('DAL',now-timedelta(minutes=5*i),1000,2)
    out=f.forecast('DAL',now,60)
    assert out.orders>0
    assert out.weight_kg>0
    assert out.confidence>0
