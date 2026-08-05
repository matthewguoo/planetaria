"""Baseline: the full pre-Alembic schema (trade_plans including the five
columns the retired _ADDITIVE_COLUMNS shim used to add, app_settings,
plan_events). Mirrors app/models/trade.py exactly as of adoption; the
test-suite parity check (tests/test_migrations.py) holds the chain and
Base.metadata to byte-equivalent column sets from here forward.

Revision ID: 0001
Revises:
Create Date: 2026-08-04
"""
import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_plans",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("underlying", sa.String(12), nullable=False),
        sa.Column("strategy", sa.String(24), nullable=False),
        sa.Column("legs", sa.JSON(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("entry_limit", sa.Float(), nullable=False),
        sa.Column("tp_premium", sa.Float(), nullable=False),
        sa.Column("sl_premium", sa.Float(), nullable=False),
        sa.Column("time_stop_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filled_qty", sa.Integer(), nullable=True),
        sa.Column("entry_order_id", sa.String(64), nullable=True),
        sa.Column("exit_order_id", sa.String(64), nullable=True),
        sa.Column("tp_order_id", sa.String(48), nullable=True),
        sa.Column("exit_reason", sa.String(24), nullable=True),
        sa.Column("fill_premium", sa.Float(), nullable=True),
        sa.Column("exit_premium", sa.Float(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_fills", sa.JSON(), nullable=True),
        sa.Column("exec_quality", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_trade_plans_status", "trade_plans", ["status"])

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(48), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "plan_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_id", sa.String(32), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("source_status", sa.String(24), nullable=False),
        sa.Column("target_status", sa.String(24), nullable=True),
        sa.Column("applied", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_plan_events_ts", "plan_events", ["ts"])
    op.create_index("ix_plan_events_plan_id", "plan_events", ["plan_id"])


def downgrade() -> None:
    op.drop_table("plan_events")
    op.drop_table("app_settings")
    op.drop_table("trade_plans")
