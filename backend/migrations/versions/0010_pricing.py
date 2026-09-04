"""A plan remembers how its orders are priced.

trade_plans.pricing JSON — {work_spread: bool | None, entry: {...}} — is
stamped at placement so the exit ladder and the entry chase honour the
choice the trader made on the ticket (or the global spread_optimizer
setting at that moment), and so a restart mid-chase resumes the same
ladder instead of forgetting where it was.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.add_column(sa.Column("pricing", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.drop_column("pricing")
