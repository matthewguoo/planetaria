"""AccountService: paper-only key pool, persistent selection, switch guards."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.system_state import AccountService


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database()
    await database.connect(f"sqlite+aiosqlite:///{tmp_path}/acct.db")
    yield database
    await database.close()


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
