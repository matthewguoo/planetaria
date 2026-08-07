"""Time-stop-only plans, and per-strategy circuit breakers.

Two halves of one decision. The PEAD research finds that a stop sitting inside
the whipsaw band of a name that just moved 5% is where most of the strategy's
edge goes — 38.3bp per trade with the shipped bracket against 138.6bp without
it. Running that strategy honestly means plans with no TP and no SL, so both
columns become nullable.

That removes a per-position loss bound, so the bound moves up a level: each
strategy instance gets a drawdown circuit breaker that flattens its whole book
and pauses it. Per-position stops protect one trade; a breaker protects the
strategy, which is the unit the operator actually allocates to.

Nulling ONE bracket is not meaningful and is refused at placement — this
migration only makes the pair optional.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-07
"""
import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.alter_column("tp_premium", existing_type=sa.Float(), nullable=True)
        batch.alter_column("sl_premium", existing_type=sa.Float(), nullable=True)
    with op.batch_alter_table("strategy_instances") as batch:
        batch.add_column(sa.Column("circuit_breaker", sa.JSON, nullable=True))


def downgrade() -> None:
    # Bracketless plans cannot be represented once the columns are NOT NULL
    # again, so give them a bracket that can never fire rather than dropping
    # rows or inventing a stop that would.
    op.execute("UPDATE trade_plans SET tp_premium = entry_limit * 20 "
               "WHERE tp_premium IS NULL")
    op.execute("UPDATE trade_plans SET sl_premium = 0 WHERE sl_premium IS NULL")
    with op.batch_alter_table("trade_plans") as batch:
        batch.alter_column("tp_premium", existing_type=sa.Float(), nullable=False)
        batch.alter_column("sl_premium", existing_type=sa.Float(), nullable=False)
    with op.batch_alter_table("strategy_instances") as batch:
        batch.drop_column("circuit_breaker")
