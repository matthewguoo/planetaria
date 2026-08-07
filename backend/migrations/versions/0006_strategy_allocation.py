"""Per-strategy capital allocation: how much of the account an instance may
deploy. Nullable, so rows written before this migration read as the default
(10% of equity) rather than as zero — an existing instance must not become
one that can trade nothing the moment the column appears.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07
"""
import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_instances") as batch:
        batch.add_column(sa.Column("allocation", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("strategy_instances") as batch:
        batch.drop_column("allocation")
