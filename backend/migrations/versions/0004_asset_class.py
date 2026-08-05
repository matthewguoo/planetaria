"""Equity support on trade_plans: asset_class (multiplier discriminator),
strategy_id (runtime provenance), extended_hours (equity DAY limits).
Server defaults make every existing row a correct option plan.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-04
"""
import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.add_column(sa.Column("asset_class", sa.String(8), nullable=False,
                                   server_default="option"))
        batch.add_column(sa.Column("strategy_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("extended_hours", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.drop_column("extended_hours")
        batch.drop_column("strategy_id")
        batch.drop_column("asset_class")
