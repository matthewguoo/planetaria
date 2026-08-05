"""Strategy registry. Explicit imports, no entry-point magic — this is a
single-owner codebase and the composition-root style is explicit everywhere.
Add a module, import its class, put it in REGISTRY."""

from app.strategies.base import Strategy

REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not getattr(cls, "kind", None):
        raise ValueError(f"{cls.__name__} has no kind")
    if cls.kind in REGISTRY:
        raise ValueError(f"duplicate strategy kind {cls.kind!r}")
    REGISTRY[cls.kind] = cls
    return cls
