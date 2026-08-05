"""Signal journal for the strategy data plane (see app/models/signals.py).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04
"""
import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column("type", sa.String(24), nullable=False),
        sa.Column("key", sa.String(128), nullable=True),
        sa.Column("symbols", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=True),
    )
    op.create_index("ix_signals_ts", "signals", ["ts"])
    op.create_index("ux_signals_source_key", "signals", ["source", "key"], unique=True)


def downgrade() -> None:
    op.drop_table("signals")
