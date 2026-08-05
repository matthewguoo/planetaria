"""Multi-account: stamp each plan with the named paper account that held
it (AccountService). Legacy rows stay NULL (single-account era).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""
import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.add_column(sa.Column("account", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.drop_column("account")
