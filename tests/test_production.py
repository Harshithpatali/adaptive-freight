import importlib

def test_auth_disabled_by_default(monkeypatch):
    monkeypatch.delenv("API_KEYS", raising=False)
    from src import config as config_mod
    importlib.reload(config_mod)
    assert config_mod.settings.auth_enabled is False

def test_auth_role_parsing(monkeypatch):
    monkeypatch.setenv("API_KEYS", "sk_admin:admin,sk_view:readonly,sk_bare")
    from src import config as config_mod
    importlib.reload(config_mod)
    keys=config_mod.settings.api_keys
    assert keys["sk_admin"]=="admin"
    assert keys["sk_view"]=="readonly"
    assert keys["sk_bare"]=="admin"
    monkeypatch.delenv("API_KEYS",raising=False)
    importlib.reload(config_mod)

def test_order_idempotency(tmp_path,monkeypatch):
    db_path=tmp_path/"test.db"; monkeypatch.setenv("DATABASE_URL",f"sqlite:///{db_path}")
    from src import persistence as p
    importlib.reload(p); p.init_db()
    assert p.record_order_if_new("SH-1",{"x":1}) is True
    assert p.record_order_if_new("SH-1",{"x":1}) is False

def test_checkpoint_roundtrip(tmp_path,monkeypatch):
    db_path=tmp_path/"test2.db"; monkeypatch.setenv("DATABASE_URL",f"sqlite:///{db_path}")
    from src import persistence as p
    importlib.reload(p); p.init_db(); assert p.load_latest_checkpoint() is None
    p.save_checkpoint({"idx":5,"sim_time":"2026-01-01T00:00:00"})
    loaded=p.load_latest_checkpoint(); assert loaded["idx"]==5
