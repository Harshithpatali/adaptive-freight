from api.main import APP
from src.config import settings


def test_api_exposes_core_routes():
    paths = {getattr(route, "path", "") for route in APP.routes}
    required = {"/health", "/state", "/metrics", "/ws", "/control/live-stream/{state}"}
    assert required.issubset(paths)


def test_streaming_defaults_are_bandwidth_safe():
    assert settings.ws_state_interval_s >= 1.0
    assert settings.ws_max_clients >= 1


def test_live_stream_interval_is_positive():
    assert settings.live_order_interval_s > 0


def test_dashboard_dependency_is_declared():
    requirements = open("requirements.txt", encoding="utf-8").read().lower()
    assert "streamlit" in requirements
