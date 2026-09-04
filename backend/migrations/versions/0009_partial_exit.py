"""A plan can be closed in parts.

Manual partial closes (close N of M from the phone) need one place to hold
the in-flight order so a restart can resolve it: trade_plans.partial_exit
JSON — {key, qty, limit, order_id, ts}, cleared once the fill (or death)
is absorbed. The filled part lands in exit_fills as a wave with
kind="partial" and its realized P/L, and the plan keeps running for the
remainder with its stop intact.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.add_column(sa.Column("partial_exit", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.drop_column("partial_exit")
