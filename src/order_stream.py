from __future__ import annotations
from datetime import datetime,timedelta
from pathlib import Path
import itertools
import numpy as np
import pandas as pd
from .config import settings

CITIES=pd.read_csv(settings.base_dir / "data/cities.csv")
HUBS=CITIES[CITIES.type=="hub"].city_id.tolist(); SPOKES=CITIES[CITIES.type=="spoke"].city_id.tolist(); ALL=CITIES.city_id.tolist()
CORRIDORS=[("DAL","HOU"),("ATL","CLT"),("CHI","IND"),("LAX","SDG"),("NYC","PHL"),("SEA","PDX"),("MIA","ORL"),("DEN","KC")]

def _sample_order(rng,shipment_id,t):
    if rng.random()<0.72:
        a,b=CORRIDORS[int(rng.integers(0,len(CORRIDORS)))]
        if rng.random()<0.25:a,b=b,a
    else:
        a=rng.choice(HUBS); b=rng.choice([x for x in SPOKES if x!=a] or [x for x in ALL if x!=a])
    w=int(np.clip(rng.lognormal(np.log(2800),0.55),400,12000)); priority="Express" if rng.random()<0.18 else "Standard"
    pickup_deadline=t+timedelta(minutes=int(rng.integers(25,75))); delivery_deadline=pickup_deadline+timedelta(minutes=int(rng.integers(90,320)))
    return {"shipment_id":shipment_id,"arrival_time":t,"pickup_city":a,"delivery_city":b,"weight_kg":w,"volume_m3":round(w*rng.uniform(.0035,.008),2),
            "revenue_usd":round(w*rng.uniform(.32,.62)+rng.uniform(90,240),2),"pickup_deadline":pickup_deadline,"delivery_deadline":delivery_deadline,"priority":priority,"status":"pending"}

def generate_orders(n:int=5000,start="2026-09-26 06:00:00",seed=42):
    rng=np.random.default_rng(seed); t=datetime.fromisoformat(start); rows=[]
    for i in range(n):
        gap_seconds=int(rng.integers(2,13))
        if i:t+=timedelta(seconds=gap_seconds)
        rows.append(_sample_order(rng,f"SH-{i+1:06d}",t))
    return pd.DataFrame(rows)

def save_order_stream(n=5000,path="data/order_stream.csv"):
    df=generate_orders(n); Path(path).parent.mkdir(parents=True,exist_ok=True); df.to_csv(path,index=False); return df

def make_live_order_sampler(seed=None,id_prefix="LIVE"):
    rng=np.random.default_rng(seed); counter=itertools.count(1)
    def _next():
        n=next(counter); return _sample_order(rng,f"{id_prefix}-{n:06d}-{int(datetime.now().timestamp())}",datetime.now())
    return _next
