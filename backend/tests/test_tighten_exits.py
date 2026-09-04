"""tighten_exits on a plan that has no stop yet: a stop may be ADDED (from
unbounded to bounded is a tightening), a target alone is refused, and the
existing loosen/ordering rules still hold."""

import pytest

from tests.test_partial_close import _held_plan, rig  # noqa: F401  (fixture + helper)


@pytest.mark.asyncio
async def test_a_stop_can_be_added_to_a_bracketless_plan(rig):  # noqa: F811
    plan = await _held_plan(rig.db, qty=1, tp=None, sl=None)
    assert plan.sl_premium is None and plan.tp_premium is None
    with pytest.raises(ValueError, match="set a stop first"):
        await rig.enforcer.tighten_exits(plan.id, tp=2.0, sl=None, time_stop_utc=None)
    updated = await rig.enforcer.tighten_exits(plan.id, tp=None, sl=0.6, time_stop_utc=None)
    assert updated.sl_premium == 0.6 and updated.tp_premium is None
    updated = await rig.enforcer.tighten_exits(plan.id, tp=2.0, sl=None, time_stop_utc=None)
    assert updated.tp_premium == 2.0 and updated.sl_premium == 0.6
    with pytest.raises(ValueError, match="only move up"):
        await rig.enforcer.tighten_exits(plan.id, tp=None, sl=0.5, time_stop_utc=None)
    with pytest.raises(ValueError, match="below TP"):
        await rig.enforcer.tighten_exits(plan.id, tp=None, sl=2.5, time_stop_utc=None)
    updated = await rig.enforcer.tighten_exits(plan.id, tp=None, sl=0.9, time_stop_utc=None)
    assert updated.sl_premium == 0.9


@pytest.mark.asyncio
async def test_stop_and_target_can_be_added_together(rig):  # noqa: F811
    plan = await _held_plan(rig.db, qty=1, tp=None, sl=None)
    updated = await rig.enforcer.tighten_exits(plan.id, tp=2.5, sl=0.7, time_stop_utc=None)
    assert (updated.sl_premium, updated.tp_premium) == (0.7, 2.5)
