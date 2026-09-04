"""The admin window's data: log tail, git head, the cross-plan journal, and
the three routes against a stubbed app state."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import admin as admin_mod
from app.api.routes.admin import router, tail_lines
from app.models.trade import PlanEventRow
from app.services.plan_fsm import PlanStateMachine


def test_tail_lines_reads_only_the_tail(tmp_path):
    p = tmp_path / "engine.log"
    p.write_text("\n".join(f"line {i}" for i in range(1, 2001)) + "\n", encoding="utf-8")
    assert tail_lines(p, 3) == ["line 1998", "line 1999", "line 2000"]
    small = tail_lines(p, 5, max_bytes=60)
    assert small[-1] == "line 2000" and all(s.startswith("line") for s in small)
    assert tail_lines(tmp_path / "missing.log", 5) == []


def test_git_head_reads_ref_or_packed(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("0123456789abcdef\n")
    assert admin_mod._git_head(tmp_path) == "0123456"
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs\nfedcba9876543210 refs/heads/main\n")
    assert admin_mod._git_head(tmp_path) == "fedcba9"
    assert admin_mod._git_head(tmp_path / "nowhere") is None


@pytest.mark.asyncio
async def test_events_recent_spans_plans(db):
    fsm = PlanStateMachine(db, SimpleNamespace(publish=lambda *a, **k: None))
    async with db.session() as s:
        for i, plan in enumerate(("p1", "p2", "p1")):
            s.add(PlanEventRow(ts=datetime.now(timezone.utc), plan_id=plan, event=f"e{i}",
                               source_status="planned", target_status="submitted", applied=1))
        await s.commit()
    rows = await fsm.events_recent(2)
    assert [r["event"] for r in rows] == ["e2", "e1"]
    assert {r["plan_id"] for r in await fsm.events_recent(10)} == {"p1", "p2"}


def _app(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(router)

    async def fake_system_state(_st):
        return {"tasks": {"reconcile-loop": "running"}, "db": {"ok": True}}

    monkeypatch.setattr(admin_mod, "system_state", fake_system_state)
    log = tmp_path / "engine.log"
    log.write_text("boot\nreconcile complete\n", encoding="utf-8")
    monkeypatch.setenv("PLANETARIA_LOG_DIR", str(tmp_path))

    class Trade:
        fsm = SimpleNamespace(events_recent=lambda limit: _events(limit))

        async def get_account(self):
            return {"equity": 10_000.0, "status": "ACTIVE"}

    async def _events(limit):
        return [{"id": 1, "event": "entry_filled", "plan_id": "p1"}][:limit]

    class Risk:
        async def open_plans(self):
            return [1, 2]

    app.state.settings = SimpleNamespace(trading_mode="live_manual", alpaca_paper=False, alpaca_account_name="live_roth")
    app.state.trade = Trade()
    app.state.risk = Risk()
    app.state.capabilities = None
    return app


def test_routes(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path, monkeypatch))
    s = client.get("/api/admin/summary").json()
    assert s["server"]["mode"] == "live_manual" and s["server"]["account"] == "live_roth"
    assert s["server"]["uptime_s"] >= 0 and s["process"]["tasks"] >= 1
    assert s["account"]["equity"] == 10_000.0 and s["open_plans"] == 2
    assert s["system"]["tasks"]["reconcile-loop"] == "running"
    assert "counts" in s["calls"]
    e = client.get("/api/admin/events?limit=5").json()
    assert e["events"][0]["event"] == "entry_filled"
    lg = client.get("/api/admin/log?lines=1").json()
    assert lg["lines"] == ["reconcile complete"] and lg["path"].endswith("engine.log")
    monkeypatch.setenv("PLANETARIA_LOG_DIR", "off")
    assert client.get("/api/admin/log").json() == {"path": None, "lines": []}
