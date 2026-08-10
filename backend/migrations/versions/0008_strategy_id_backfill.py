"""Backfill trade_plans.strategy_id from the display label, then stop
joining on it.

Every query that linked a plan back to its strategy joined on
TradePlan.strategy — the 24-char display label — while the strategy_id FK
sat populated on every strategy-placed plan since 0006. The label was never
a safe key: the retired discretionary terminal wrote leg-structure names
("iron_condor", "put_butterfly") into the same column, so the join was
correct only while no structure name collided with an instance name.

This migration closes the one gap a pure code change would leave: plans
placed by a strategy before the FK existed carry the instance name in the
label with a NULL strategy_id. Those get the FK stamped where the label
matches exactly one instance. Terminal-era plans match no instance and stay
NULL, which is the truth — no strategy owns them.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-10
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Correlated subquery over the instances table; portable across the
    # Postgres store and the SQLite fallback.
    op.execute(
        """
        UPDATE trade_plans
        SET strategy_id = (
            SELECT si.id FROM strategy_instances si
            WHERE si.name = trade_plans.strategy
        )
        WHERE strategy_id IS NULL
          AND strategy IN (SELECT name FROM strategy_instances)
        """
    )


def downgrade() -> None:
    # The backfilled rows are indistinguishable from FK-native ones by
    # design, and un-stamping a correct link would only re-break the joins.
    # The label column is untouched, so the pre-0008 read path still works.
    pass
