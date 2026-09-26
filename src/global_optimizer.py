from __future__ import annotations
from typing import Iterable

try:
    from ortools.sat.python import cp_model
except Exception:
    cp_model=None

class GlobalFleetOptimizer:
    """Periodic fleet-to-warehouse repositioning optimizer.

    Uses CP-SAT when OR-Tools is available. It minimizes repositioning distance
    while rewarding coverage of warehouses with forecast demand.
    """
    def __init__(self):
        self.runs=0
        self.last_status="disabled"

    def optimize(self, vehicles: Iterable, forecasts: list[dict], distance_fn, max_repositions: int = 3):
        self.runs += 1
        if cp_model is None or not forecasts:
            self.last_status="unavailable"
            return []

        idle=[v for v in vehicles if v.status=="idle"]
        if not idle:
            self.last_status="no_idle_vehicles"
            return []

        warehouses=[f for f in forecasts if f.get("forecast_orders",0)>0]
        if not warehouses:
            self.last_status="no_forecast_demand"
            return []

        model=cp_model.CpModel()
        x={}
        for i,v in enumerate(idle):
            for j,w in enumerate(warehouses):
                x[i,j]=model.NewBoolVar(f"x_{i}_{j}")

        for i,_ in enumerate(idle):
            model.Add(sum(x[i,j] for j in range(len(warehouses))) <= 1)
        for j,w in enumerate(warehouses):
            capacity=max(0,min(max_repositions,int(round(w["forecast_orders"]/4.0)+1)))
            model.Add(sum(x[i,j] for i in range(len(idle))) <= capacity)

        terms=[]
        for i,v in enumerate(idle):
            for j,w in enumerate(warehouses):
                km=float(distance_fn(v.current_city,w["warehouse"]))
                demand=float(w["forecast_orders"])
                value=int(max(1,min(100000, demand*1000 - km*10)))
                terms.append((value,x[i,j]))
        model.Maximize(sum(value*var for value,var in terms))
        solver=cp_model.CpSolver()
        solver.parameters.max_time_in_seconds=0.15
        solver.parameters.num_search_workers=1
        status=solver.Solve(model)
        if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            self.last_status="no_solution"
            return []

        result=[]
        for i,v in enumerate(idle):
            for j,w in enumerate(warehouses):
                if solver.Value(x[i,j]) == 1:
                    km=float(distance_fn(v.current_city,w["warehouse"]))
                    result.append({"vehicle_id":v.vehicle_id,"warehouse":w["warehouse"],"distance_km":round(km,1),
                                   "forecast_orders":w["forecast_orders"],"confidence":w["confidence"]})
        self.last_status="feasible"
        return result
