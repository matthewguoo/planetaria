"""TRADING_MODE=live_manual: the isolated live server.

Three invariants under test, each a structural refusal rather than a flag:
  1. config: the paper server's lock is byte-for-byte intact, and a live
     boot dies on any missing isolation (strategies on, no pinned account,
     default DB/redis URLs).
  2. bootstrap/main: the strategy plane is never constructed and the
     strategies API answers 409 — the only entry path is the human ticket.
  3. place_trade: live entries refuse automation ids and anything the
     level-2 options account cannot hold.
"""

import importlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.config import Settings
from app.db.session import Database
from app.services.risk import RiskService
from app.services.trade_service import TradeService

LIVE_OK = dict(
    trading_mode="live_manual",
    strategies_enabled=False,
    live_account_name="live_roth",
    database_url="postgresql+asyncpg://trader:trader@localhost:5433/trader_live",
    redis_url="redis://localhost:6380/1",
)


def make(**over) -> Settings:
    # _env_file=None: never read the real .env in the config matrix.
    return Settings(_env_file=None, **over)


# ---------------------------------------------------------------- config


class TestPaperLockUnchanged:
    def test_paper_default_boots(self):
        make().validate_paper_lock()

    def test_paper_refuses_live_flag(self):
        with pytest.raises(RuntimeError, match="paper server"):
            make(alpaca_paper=False).validate_paper_lock()


class TestLiveBootLock:
    def test_valid_live_forces_paper_false(self):
        s = make(**LIVE_OK)
        s.validate_paper_lock()
        assert s.alpaca_paper is False

    def test_alpaca_paper_env_is_ignored_on_live(self):
        # Derived, never trusted: even ALPACA_PAPER=true cannot make the
        # live server talk to the paper endpoint (a false sense of safety).
        s = make(**LIVE_OK, alpaca_paper=True)
        s.validate_paper_lock()
        assert s.alpaca_paper is False

    def test_refuses_strategies_enabled(self):
        with pytest.raises(RuntimeError, match="STRATEGIES_ENABLED=false"):
            make(**{**LIVE_OK, "strategies_enabled": True}).validate_paper_lock()

    @pytest.mark.parametrize("name", ["", "roth", "paper_roth"])
    def test_refuses_unpinned_or_misnamed_account(self, name):
        with pytest.raises(RuntimeError, match="LIVE_ACCOUNT_NAME"):
            make(**{**LIVE_OK, "live_account_name": name}).validate_paper_lock()

    def test_refuses_default_database_url(self):
        over = {**LIVE_OK, "database_url": Settings.model_fields["database_url"].default}
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            make(**over).validate_paper_lock()

    def test_refuses_default_redis_url(self):
        over = {**LIVE_OK, "redis_url": Settings.model_fields["redis_url"].default}
        with pytest.raises(RuntimeError, match="REDIS_URL"):
            make(**over).validate_paper_lock()


# ------------------------------------------------------- strategy plane


def test_strategy_plane_only_on_paper():
    from app.bootstrap import strategy_plane_enabled

    assert strategy_plane_enabled(SimpleNamespace(trading_mode="paper")) is True
    assert strategy_plane_enabled(SimpleNamespace(trading_mode="live_manual")) is False
    # Legacy settings objects without the field read as paper.
    assert strategy_plane_enabled(SimpleNamespace()) is True


_LIVE_ENV = {
    "TRADING_MODE": "live_manual",
    "STRATEGIES_ENABLED": "false",
    "LIVE_ACCOUNT_NAME": "live_roth",
    "DATABASE_URL": LIVE_OK["database_url"],
    "REDIS_URL": LIVE_OK["redis_url"],
}


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


def _reload_main(monkeypatch, env: dict[str, str]):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import config

    config.get_settings.cache_clear()
    import app.main

    importlib.reload(app.main)
    return app.main


def teardown_module(_module):
    for k in _LIVE_ENV:
        os.environ.pop(k, None)
    from app import config

    config.get_settings.cache_clear()
    import app.main

    importlib.reload(app.main)


def test_live_server_stubs_strategies_with_409(monkeypatch):
    from fastapi.testclient import TestClient

    main = _reload_main(monkeypatch, _LIVE_ENV)
    # No lifespan (no `with`): nothing boots, the route table alone answers.
    client = TestClient(main.app)
    for path in ("/api/strategies", "/api/strategies/abc/enable",
                 "/api/strategies/kill-all", "/api/signals", "/api/fund"):
        r = client.post(path, json={})
        assert r.status_code == 409, (path, r.status_code)
        assert "manual entries" in r.json()["detail"]
    assert client.get("/api/strategies").status_code == 409
    health = client.get("/api/health").json()
    assert health["mode"] == "live_manual" and health["paper"] is False


def test_live_server_keeps_manual_engine_routes(monkeypatch):
    main = _reload_main(monkeypatch, _LIVE_ENV)
    paths = _flat_paths(main.app.routes)
    for p in ("/api/orders", "/api/positions", "/api/positions/adopt",
              "/api/positions/{plan_id}/close", "/api/positions/flatten",
              "/api/system/state", "/api/settings/risk"):
        assert p in paths, p
    # The REAL strategies surface is gone, not merely shadowed.
    assert "/api/strategies/{instance_id}/enable" not in paths
    assert "/api/strategies/catalog" not in paths


def test_paper_server_mounts_real_strategies(monkeypatch):
    for k in _LIVE_ENV:
        monkeypatch.delenv(k, raising=False)
    main = _reload_main(monkeypatch, {"TRADING_MODE": "paper"})
    paths = _flat_paths(main.app.routes)
    assert "/api/strategies/catalog" in paths


# ------------------------------------------------------ place_trade gates


class _Broadcast:
    def publish(self, topic, msg):
        pass


@pytest_asyncio.fixture
async def live_trade(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    alpaca = SimpleNamespace(
        configured=True,
        settings=SimpleNamespace(trading_mode="live_manual", alpaca_paper=False,
                                 alpaca_account_name="live_roth"),
    )
    market = SimpleNamespace(broadcast=_Broadcast())
    svc = TradeService(db, alpaca, market, RiskService(db))
    yield svc
    await db.close()


def _option_payload(legs, **over):
    ts = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    base = dict(underlying="SPY", strategy="long_call", legs=legs, qty=1,
                entry_limit=1.0, tp_premium=2.0, sl_premium=0.5, time_stop_utc=ts)
    base.update(over)
    return base


def _leg(side, strike):
    return {"symbol": f"SPY260918C00{int(strike)}000", "right": "C",
            "strike": strike, "expiry": "2026-09-18", "side": side,
            "ratio": 1, "entry": 1.0, "iv": 0.2}


@pytest.mark.asyncio
async def test_live_refuses_automation_ids(live_trade):
    with pytest.raises(ValueError, match="automation ids"):
        await live_trade.place_trade(_option_payload([_leg(1, 450)], strategy_id="pead-1"))


@pytest.mark.asyncio
async def test_live_refuses_spreads_and_short_legs(live_trade):
    with pytest.raises(ValueError, match="level 2"):
        await live_trade.place_trade(_option_payload([_leg(1, 450), _leg(-1, 455)]))
    with pytest.raises(ValueError, match="level 2"):
        await live_trade.place_trade(_option_payload([_leg(-1, 450)]))


@pytest.mark.asyncio
async def test_live_gate_passes_long_single_leg_to_normal_validation(live_trade):
    # The gate lets it through; downstream validation then runs as on paper
    # (here: no quote/market, so it fails LATER than the gate, proving the
    # gate itself did not fire).
    try:
        await live_trade.place_trade(_option_payload([_leg(1, 450)]))
    except ValueError as exc:
        assert "level 2" not in str(exc) and "automation" not in str(exc)
    except Exception:
        pass  # anything past validation is fine for this test's purpose


@pytest.mark.asyncio
async def test_live_get_account_reports_mode(live_trade):
    live_trade.alpaca.configured = False
    acct = await live_trade.get_account()
    assert acct["mode"] == "live_manual" and acct["paper"] is False


class TestKeylessPreview:
    """KEYLESS=true is the UI-preview knob: whatever .env holds, the process
    boots with no broker keys and an empty account registry, and it is
    refused outside the paper server."""

    def test_keyless_blanks_keys_from_any_source(self):
        s = make(keyless=True, alpaca_api_key="PKREAL", alpaca_secret_key="sekrit")
        s.validate_paper_lock()
        assert (s.alpaca_api_key, s.alpaca_secret_key) == ("", "")

    def test_keyless_registry_is_empty(self, monkeypatch):
        from app.services.system_state import AccountService

        monkeypatch.setenv("ALPACA_ACCOUNT_PREVIEW_API_KEY", "PKX")
        monkeypatch.setenv("ALPACA_ACCOUNT_PREVIEW_SECRET_KEY", "s")
        s = make(keyless=True)
        assert AccountService(db=None, settings=s).registry() == {}
        assert "preview" in AccountService(db=None, settings=make()).registry()

    def test_keyless_refused_on_the_live_server(self):
        with pytest.raises(RuntimeError, match="KEYLESS"):
            make(**{**LIVE_OK, "keyless": True}).validate_paper_lock()
