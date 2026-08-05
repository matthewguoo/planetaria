"""Strategy registry + decision journal (see app/models/strategies.py).

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-04
"""
import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_instances",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("name", sa.String(24), nullable=False, unique=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "strategy_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_id", sa.String(32),
                  sa.ForeignKey("strategy_instances.id"), nullable=False),
        sa.Column("signal_ids", sa.JSON(), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=True),
        sa.Column("plan_id", sa.String(32), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_decisions_strategy_ts", "strategy_decisions", ["strategy_id", "ts"]
    )


def downgrade() -> None:
    op.drop_table("strategy_decisions")
    op.drop_table("strategy_instances")
