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


# Built-in strategies self-register on import. Bottom of the module so the
# decorator above exists; add new strategy modules here.
#
# Retired 2026-08-07: `ref_tick` and `llm_probe` were integration proofs that
# had served their purpose, and `earnings_reaction` was the control
# configuration the paper measures and argues against — `pead_flagship` runs
# the policy it argues FOR. The paper's Table 2 no longer reads that class
# live; the values it documented are frozen in the research tree, with the
# reasoning at research/pead-llm-gate/scripts/_shipped_config.py.
from app.strategies import afternoon_fly  # noqa: E402,F401
from app.strategies import mech_carry  # noqa: E402,F401
from app.strategies import pead_flagship  # noqa: E402,F401
from app.strategies import pead_nosip  # noqa: E402,F401
