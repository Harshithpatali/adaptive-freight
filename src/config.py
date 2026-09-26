from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

def _split_csv(v: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in v.split(",") if x.strip())

@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8501"))
    router_url: str = os.getenv("ROUTER_URL", "https://router.project-osrm.org").rstrip("/")
    public_router_fallback: bool = os.getenv("PUBLIC_ROUTER_FALLBACK", "true").lower() == "true"
    redis_url: str = os.getenv("REDIS_URL", "").strip()
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/adaptive_freight.db")
    sim_minutes_per_second: float = float(os.getenv("SIM_MINUTES_PER_SECOND", "2.0"))
    engine_tick_ms: int = int(os.getenv("ENGINE_TICK_MS", "200"))
    max_orders_per_tick: int = int(os.getenv("MAX_ORDERS_PER_TICK", "50"))
    route_timeout_seconds: float = float(os.getenv("ROUTE_TIMEOUT_SECONDS", "2.0"))
    route_cache_size: int = int(os.getenv("ROUTE_CACHE_SIZE", "4096"))
    traffic_factor: float = float(os.getenv("TRAFFIC_FACTOR", "1.0"))
    api_keys_raw: str = os.getenv("API_KEYS", "").strip()
    cors_origins: tuple[str, ...] = field(default_factory=lambda: _split_csv(os.getenv("CORS_ORIGINS", "")))
    demo_order_stream: bool = os.getenv("DEMO_ORDER_STREAM", "true").lower() == "true"
    checkpoint_interval_s: float = float(os.getenv("CHECKPOINT_INTERVAL_S", "10"))
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "300"))
    log_json: bool = os.getenv("LOG_JSON", "false").lower() == "true"
    live_order_stream: bool = os.getenv("LIVE_ORDER_STREAM", "true").lower() == "true"
    live_order_interval_s: float = float(os.getenv("LIVE_ORDER_INTERVAL_S", "15"))

    @property
    def api_keys(self) -> dict[str, str]:
        out = {}
        for pair in _split_csv(self.api_keys_raw):
            if ":" in pair:
                key, role = pair.split(":", 1)
            else:
                key, role = pair, "admin"
            out[key.strip()] = role.strip() or "admin"
        return out

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

settings = Settings()
