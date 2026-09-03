"""AccountService: paper-only key pool, persistent selection, switch guards."""

import os
from types import SimpleNamespace

import pytest

from app.services.system_state import AccountService


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Never read the real .env in tests: actual keys would leak into
    assertion diffs. os.environ stays live so setenv-based cases work."""
    monkeypatch.setattr(AccountService, "_env_sources",
                        lambda self: dict(os.environ))
    for var in [v for v in os.environ if v.startswith("ALPACA")]:
        monkeypatch.delenv(var, raising=False)


def settings(**over):
    base = dict(alpaca_api_key="PKDEFAULT000", alpaca_secret_key="s-default")
    base.update(over)
    return SimpleNamespace(**base)


class TestRegistry:
    def test_env_parse_and_paper_gate(self, db, monkeypatch):
        s = settings()
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_API_KEY", "PKNEW111")
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_SECRET_KEY", "s-new")
        monkeypatch.setenv("ALPACA_ACCOUNT_EVIL_API_KEY", "AKLIVE999")
        monkeypatch.setenv("ALPACA_ACCOUNT_EVIL_SECRET_KEY", "s-live")
        monkeypatch.setenv("ALPACA_ACCOUNT_HALF_API_KEY", "PKHALF")  # no secret
        reg = AccountService(db, s).registry()
        assert reg["default"]["api_key"] == "PKDEFAULT000"
        assert reg["planetaria1"] == {"api_key": "PKNEW111",
                                      "secret_key": "s-new"}
        assert "evil" not in reg    # live key refused: the paper gate
        assert "half" not in reg    # keyless half-pair refused

    def test_live_default_key_is_refused(self, db):
        reg = AccountService(db, settings(alpaca_api_key="AKLIVE")).registry()
        assert "default" not in reg


@pytest.mark.asyncio
class TestSelection:
    async def test_apply_default_then_switch_persists(self, db, monkeypatch):
        s = settings()
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_API_KEY", "PKNEW111")
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_SECRET_KEY", "s-new")
        svc = AccountService(db, s)
        assert await svc.apply() == "default"
        assert s.alpaca_account_name == "default"

        out = await svc.select("planetaria1", open_plans=0)
        assert out["selected"] == "planetaria1"
        assert out["restart_required"] is True  # applied is still default

        # New boot (fresh service): the persisted selection applies.
        s2 = settings()
        svc2 = AccountService(db, s2)
        assert await svc2.apply() == "planetaria1"
        assert s2.alpaca_api_key == "PKNEW111"
        assert s2.alpaca_secret_key == "s-new"
        assert (await svc2.list_accounts())["restart_required"] is False

    async def test_switch_refused_with_open_plans(self, db, monkeypatch):
        s = settings()
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_API_KEY", "PKNEW111")
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_SECRET_KEY", "s-new")
        svc = AccountService(db, s)
        await svc.apply()
        with pytest.raises(ValueError, match="open plan"):
            await svc.select("planetaria1", open_plans=3)

    async def test_unknown_account_refused(self, db):
        svc = AccountService(db, settings())
        with pytest.raises(ValueError, match="no keys"):
            await svc.select("nope", open_plans=0)

    async def test_missing_keys_falls_back_to_default(self, db, monkeypatch):
        s = settings()
        monkeypatch.setenv("ALPACA_ACCOUNT_GHOST_API_KEY", "PKGHOST")
        monkeypatch.setenv("ALPACA_ACCOUNT_GHOST_SECRET_KEY", "s-ghost")
        svc = AccountService(db, s)
        await svc.select("ghost", open_plans=0)
        monkeypatch.delenv("ALPACA_ACCOUNT_GHOST_API_KEY")
        monkeypatch.delenv("ALPACA_ACCOUNT_GHOST_SECRET_KEY")
        s2 = settings()
        assert await AccountService(db, s2).apply() == "default"
        assert s2.alpaca_api_key == "PKDEFAULT000"


def live_settings(**over):
    """The isolated live server's settings shape: the paper 'default' pair
    is still present in .env (shared file) and must be dropped."""
    return settings(trading_mode="live_manual", live_account_name="live_roth",
                    **over)


def _live_env(monkeypatch):
    monkeypatch.setenv("ALPACA_ACCOUNT_LIVE_ROTH_API_KEY", "AKROTH111")
    monkeypatch.setenv("ALPACA_ACCOUNT_LIVE_ROTH_SECRET_KEY", "s-roth")
    monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_API_KEY", "PKNEW111")
    monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_SECRET_KEY", "s-new")
    # A live key under a name without the live_ prefix: refused too — the
    # name is part of the contract, so a paper-book name can never carry
    # real money by accident.
    monkeypatch.setenv("ALPACA_ACCOUNT_SNEAKY_API_KEY", "AKSNEAK999")
    monkeypatch.setenv("ALPACA_ACCOUNT_SNEAKY_SECRET_KEY", "s-sneak")
    # And a live_-named PAPER key: refused (wrong environment for the name).
    monkeypatch.setenv("ALPACA_ACCOUNT_LIVE_FAKE_API_KEY", "PKFAKE000")
    monkeypatch.setenv("ALPACA_ACCOUNT_LIVE_FAKE_SECRET_KEY", "s-fake")


class TestLiveRegistry:
    def test_live_pool_admits_only_live_named_ak_keys(self, db, monkeypatch):
        _live_env(monkeypatch)
        reg = AccountService(db, live_settings()).registry()
        assert set(reg) == {"live_roth"}
        assert reg["live_roth"] == {"api_key": "AKROTH111", "secret_key": "s-roth"}

    def test_paper_pool_still_drops_live_keys(self, db, monkeypatch):
        # The paper server's gate is untouched by the live mode's existence.
        _live_env(monkeypatch)
        reg = AccountService(db, settings()).registry()
        assert "live_roth" not in reg and "sneaky" not in reg
        assert set(reg) == {"default", "planetaria1", "live_fake"}


@pytest.mark.asyncio
class TestLiveApply:
    async def test_apply_pins_env_account_and_ignores_db_selection(self, db, monkeypatch):
        _live_env(monkeypatch)
        # A stale paper selection in the (separate) live DB must not matter.
        s = live_settings()
        svc = AccountService(db, s)
        assert await svc.apply() == "live_roth"
        assert s.alpaca_api_key == "AKROTH111"
        assert s.alpaca_secret_key == "s-roth"
        assert s.alpaca_account_name == "live_roth"
        out = await svc.list_accounts()
        assert out["paper_only"] is False
        assert out["mode"] == "live_manual"
        assert out["selected"] == out["applied"] == "live_roth"
        assert out["restart_required"] is False

    async def test_apply_dies_without_live_keys(self, db, monkeypatch):
        # NO fallback to 'default': that would leave paper keys active on a
        # process whose client is constructed paper=False.
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_API_KEY", "PKNEW111")
        monkeypatch.setenv("ALPACA_ACCOUNT_PLANETARIA1_SECRET_KEY", "s-new")
        s = live_settings()
        with pytest.raises(RuntimeError, match="refusing to boot"):
            await AccountService(db, s).apply()
        assert s.alpaca_api_key == "PKDEFAULT000"  # untouched, never applied

    async def test_select_refused_on_live(self, db, monkeypatch):
        _live_env(monkeypatch)
        svc = AccountService(db, live_settings())
        await svc.apply()
        with pytest.raises(ValueError, match="pinned"):
            await svc.select("live_roth", open_plans=0)

    async def test_paper_apply_unchanged(self, db, monkeypatch):
        _live_env(monkeypatch)
        s = settings()
        assert await AccountService(db, s).apply() == "default"
        assert s.alpaca_api_key == "PKDEFAULT000"
        assert (await AccountService(db, s).list_accounts())["paper_only"] is True
