"""Model registration: importing any submodule executes this package init,
so every table is attached to Base.metadata before create_all or Alembic
autogenerate ever look at it. Add new model modules here."""

from app.models import signals, strategies, trade  # noqa: F401
