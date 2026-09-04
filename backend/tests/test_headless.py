"""HEADLESS engine mode: command/ops API mounted, UI-serving routes absent."""

import importlib


def _flat_paths(routes) -> set[str]:
    """FastAPI mounts included routers lazily (_IncludedRouter wrapping the
    original APIRouter); walk both plain routes and wrapped routers."""
    out: set[str] = set()
    for r in routes:
        path = getattr(r, "path", None)
        if path:
            out.add(path)
        inner = getattr(r, "original_router", None)
        if inner is not None:
            out |= _flat_paths(inner.routes)
        sub = getattr(r, "routes", None)
        if sub:
            out |= _flat_paths(sub)
    return out


def _routes_with_env(monkeypatch, headless: bool) -> set[str]:
    monkeypatch.setenv("HEADLESS", "true" if headless else "false")
    from app import config

    config.get_settings.cache_clear()
    import app.main

    importlib.reload(app.main)
    return _flat_paths(app.main.app.routes)


def teardown_module(_module):
    # Restore the default app object for any later test importing app.main.
    import os

    os.environ.pop("HEADLESS", None)
    from app import config

    config.get_settings.cache_clear()
    import app.main

    importlib.reload(app.main)


ENGINE_PATHS = {
    "/api/orders",
    "/api/positions",
    "/api/positions/{plan_id}/close",
    "/api/positions/{plan_id}/events",
    "/api/system/state",
    "/api/settings/feed",
    "/api/settings/risk",
    "/api/health",
}
UI_PATHS = {"/ws/stream", "/api/options/chain/{underlying}", "/api/quote/{symbol}"}


def test_headless_mounts_engine_api_only(monkeypatch):
    paths = _routes_with_env(monkeypatch, headless=True)
    for p in ENGINE_PATHS:
        assert p in paths, f"engine path {p} missing in headless mode"
    for p in UI_PATHS:
        assert p not in paths, f"UI path {p} should be absent in headless mode"


def test_default_mounts_everything(monkeypatch):
    paths = _routes_with_env(monkeypatch, headless=False)
    for p in ENGINE_PATHS | UI_PATHS:
        assert p in paths, f"path {p} missing in full mode"


def _mount_names_with_env(monkeypatch, headless: bool) -> set:
    monkeypatch.setenv("HEADLESS", "true" if headless else "false")
    from app import config

    config.get_settings.cache_clear()
    import app.main

    importlib.reload(app.main)
    return {getattr(r, "name", None) for r in app.main.app.routes}


def test_headless_never_serves_static_ui(monkeypatch):
    assert "ui" not in _mount_names_with_env(monkeypatch, headless=True)


def test_full_mode_serves_static_ui_when_built(monkeypatch):
    names = _mount_names_with_env(monkeypatch, headless=False)
    import app.main

    # The mount exists exactly when a frontend build is on disk.
    assert ("ui" in names) == app.main._dist.is_dir()
