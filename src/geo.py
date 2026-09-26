from __future__ import annotations
import math
from functools import lru_cache
from typing import Iterable
import pandas as pd
from .config import settings

CITIES = pd.read_csv(settings.base_dir / "data/cities.csv").set_index("city_id")

@lru_cache(maxsize=8192)
def coords(city: str) -> tuple[float, float]:
    row = CITIES.loc[city]
    return float(row.lat), float(row.lon)

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2-lat1, lon2-lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(h))

def city_distance(a: str, b: str) -> float:
    if a == b: return 0.0
    return haversine_km(coords(a), coords(b)) * 1.11

def route_distance(cities: Iterable[str]) -> float:
    vals = list(cities)
    return round(sum(city_distance(vals[i], vals[i+1]) for i in range(len(vals)-1)), 2)

def nearest_city(lat: float, lon: float) -> str:
    p=(lat,lon)
    return min(CITIES.index, key=lambda c: haversine_km(p, coords(c)))
