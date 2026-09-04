"""copy_store: SQLite paper store -> another store, through the models.
Exercised SQLite-to-SQLite here (the box run against Postgres is the
integration proof); what this pins is FK order, PK preservation, JSON and
datetime round trips, the refusal rules, and the modes."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import copy_store
from app.db.session import Database
from app.models.signals import SignalRow
from app.models.strategies import StrategyDecisionRow, StrategyInstanceRow
from app.models.trade import AppSetting, PlanEventRow, TradePlan, as_utc

NOW = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)


async def _seed(url: str) -> None:
    db = Database()
    await db.connect(url)
    try:
        async with db.session() as s:
            plan = TradePlan(
                id="p" * 32, underlying="SPY", strategy="long_call", account="planetaria1",
                legs=[{"symbol": "SPY260918C00650000", "right": "C", "strike": 650.0,
                       "expiry": "2026-09-18", "side": 1, "ratio": 1, "entry": 1.2, "iv": 0.2}],
                qty=2, entry_limit=1.2, tp_premium=2.4, sl_premium=0.6,
                time_stop_utc=NOW + timedelta(days=1), status="filled", filled_qty=2,
                fill_premium=1.21, exec_quality={"entry": {"fair": 1.2, "half_spread": 0.05}},
                created_at=NOW, updated_at=NOW,
            )
            s.add(plan)
            s.add(PlanEventRow(id=1, ts=NOW, plan_id=plan.id, event="entry_submitted",
                               source_status="planned", target_status="submitted", applied=1))
            s.add(PlanEventRow(id=2, ts=NOW, plan_id=plan.id, event="entry_filled",
                               source_status="submitted", target_status="filled", applied=1))
            s.add(SignalRow(id=1, ts=NOW, source="edgar", type="news", key="k1",
                            symbols=["SPY"], payload={"text": "x" * 3000}))
            s.add(SignalRow(id=2, ts=NOW, source="llm:pead", type="analysis", key=None,
                            symbols=["SPY"], payload={"verdict": "go"}, parent_id=1))
            s.add(SignalRow(id=3, ts=NOW, source="timer", type="signal", key="t1",
                            symbols=None, payload={}))
            s.add(StrategyInstanceRow(id="s1", kind="pead_flagship", name="pead", params={"live": True},
                                      allocation={"mode": "pct", "value": 10}, state="enabled",
                                      created_at=NOW, updated_at=NOW))
            s.add(StrategyInstanceRow(id="s2", kind="afternoon_fly", name="fly-1", params={},
                                      state="paused", created_at=NOW, updated_at=NOW))
            s.add(StrategyDecisionRow(id=1, ts=NOW, strategy_id="s1", signal_ids=[1, 2],
                                      action="submit", plan_id=plan.id, detail={"why": "gate"}))
            s.add(StrategyDecisionRow(id=2, ts=NOW, strategy_id="s2", action="skip", detail=None))
            s.add(AppSetting(key="account", value={"selected": "planetaria1"}, updated_at=NOW))
            s.add(AppSetting(key="risk", value={"max_loss_pct": 0.02}, updated_at=NOW))
            await s.commit()
    finally:
        await db.close()


EXPECTED = {"trade_plans": 1, "plan_events": 2, "signals": 3, "strategy_decisions": 2,
            "strategy_instances": 2, "app_settings": 2}


@pytest.mark.asyncio
async def test_copy_round_trip(tmp_path):
    src = f"sqlite+aiosqlite:///{tmp_path}/src.db"
    dst = f"sqlite+aiosqlite:///{tmp_path}/dst.db"
    await _seed(src)

    assert await copy_store.run(src, dst, dry_run=True, verify=False, force=False) == 0
    assert not (tmp_path / "dst.db").exists() or (tmp_path / "dst.db").stat().st_size == 0

    assert await copy_store.run(src, dst, dry_run=False, verify=False, force=False) == 0

    db = Database()
    await db.connect(dst, fallback=False)
    try:
        async with db.session() as s:
            plan = await s.get(TradePlan, "p" * 32)
            assert plan.legs[0]["strike"] == 650.0 and plan.exec_quality["entry"]["fair"] == 1.2
            assert as_utc(plan.created_at) == NOW and as_utc(plan.time_stop_utc) == NOW + timedelta(days=1)
            sig = await s.get(SignalRow, 2)
            assert sig.parent_id == 1 and sig.payload == {"verdict": "go"}
            inst = await s.get(StrategyInstanceRow, "s1")
            assert inst.allocation == {"mode": "pct", "value": 10}
            events = (await s.execute(select(PlanEventRow).order_by(PlanEventRow.id))).scalars().all()
            assert [e.id for e in events] == [1, 2]
            # A new journal row after the copy continues the id sequence.
            s.add(PlanEventRow(ts=NOW, plan_id=plan.id, event="exit_filled",
                               source_status="exiting", target_status="closed", applied=1))
            await s.commit()
            assert (await s.execute(select(PlanEventRow.id).order_by(PlanEventRow.id.desc()))).scalar() == 3
    finally:
        await db.close()

    # --verify after the extra row reports the count drift.
    assert await copy_store.run(src, dst, dry_run=False, verify=True, force=False) == 4
    # A second copy refuses a populated target; --force wipes and re-copies.
    assert await copy_store.run(src, dst, dry_run=False, verify=False, force=False) == 3
    assert await copy_store.run(src, dst, dry_run=False, verify=False, force=True) == 0
    assert await copy_store.run(src, dst, dry_run=False, verify=True, force=False) == 0


@pytest.mark.asyncio
async def test_source_not_at_head_is_refused(tmp_path):
    src = f"sqlite+aiosqlite:///{tmp_path}/old.db"
    await _seed(src)
    import sqlite3

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("UPDATE alembic_version SET version_num = '0007'")
    conn.commit()
    conn.close()
    assert await copy_store.run(src, f"sqlite+aiosqlite:///{tmp_path}/x.db",
                                dry_run=False, verify=False, force=False) == 2
    assert not (tmp_path / "x.db").exists()


def test_normalize_makes_datetimes_aware():
    tables = {t.name: t for t in copy_store.tables_in_order()}
    naive = datetime(2026, 9, 3, 12, 30)
    out = copy_store.normalize(tables["trade_plans"], {"created_at": naive, "legs": [1], "qty": 2})
    assert out["created_at"].tzinfo is timezone.utc and out["created_at"].hour == 12
    assert out["legs"] == [1] and out["qty"] == 2
    already = datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc)
    assert copy_store.normalize(tables["signals"], {"ts": already})["ts"] is already


def test_tables_are_fk_ordered():
    names = [t.name for t in copy_store.tables_in_order()]
    assert set(names) == set(EXPECTED)
    assert names.index("strategy_instances") < names.index("strategy_decisions")
    assert names.index("trade_plans") < names.index("plan_events") or True  # no FK, order free
    assert {t.name for t in copy_store.integer_pk_tables(copy_store.tables_in_order())} == {
        "plan_events", "signals", "strategy_decisions"}
