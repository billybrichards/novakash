"""Per-strategy gate parameter lookup with env fallback.

Strategy hooks (e.g. v4_fusion.py, v5_ensemble.py) historically read
tuning knobs from module-global env vars at import time. That made
every strategy that imports a shared hook react identically when the
operator flipped a flag on Montreal — there was no way to run
v5_ensemble strict while v5_fresh runs relaxed.

This module provides a strategy-scoped override layer. Each strategy's
YAML can declare a ``gate_params:`` block; the registry sets the
contextvar around the hook call; the hook reads params via the
``get_*`` helpers, which prefer the active per-strategy dict but fall
back to the env var (and finally the hard-coded default) when the key
isn't declared in YAML.

PR 1 lands the mechanism only — v4/v5 hooks still read env directly.
PR 2 migrates the hooks to consume ``get_*`` so YAML becomes the
canonical source. Env fallback stays for 30 days as an ops escape
hatch.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

# Active gate-params bag for the strategy currently being evaluated.
# Set by StrategyRegistry._evaluate_one around the hook call; read by
# hook code via the get_* helpers. Defaults to empty dict so that
# calling get_* outside an evaluate_one scope behaves like pure
# env-only lookup (useful for tests and ad-hoc hook calls).
_ACTIVE: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "strategy_gate_params", default={}
)


class _Sentinel:
    pass


_MISSING = _Sentinel()


def set_active(params: dict[str, Any] | None) -> contextvars.Token:
    """Bind ``params`` as the active gate-params dict. Returns a token
    for ``reset_active``. Callers must reset in a try/finally.
    """
    return _ACTIVE.set(dict(params or {}))


def reset_active(token: contextvars.Token) -> None:
    _ACTIVE.reset(token)


def _lookup(name: str, env_var: str | None, default: Any) -> Any:
    """Resolve a param value: YAML override → env → default."""
    params = _ACTIVE.get()
    if name in params:
        return params[name]
    if env_var is not None:
        env_val = os.environ.get(env_var, _MISSING)
        if not isinstance(env_val, _Sentinel):
            return env_val
    return default


def get_bool(name: str, env_var: str | None, default: bool) -> bool:
    raw = _lookup(name, env_var, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def get_float(name: str, env_var: str | None, default: float) -> float:
    raw = _lookup(name, env_var, default)
    return float(raw)


def get_int(name: str, env_var: str | None, default: int) -> int:
    raw = _lookup(name, env_var, default)
    return int(raw)


def get_str(name: str, env_var: str | None, default: str) -> str:
    raw = _lookup(name, env_var, default)
    return str(raw)


def get_int_list(name: str, env_var: str | None, default: list[int]) -> list[int]:
    """Resolve a list of ints. YAML accepts [1,2,3]; env accepts '1,2,3'."""
    raw = _lookup(name, env_var, default)
    if isinstance(raw, list):
        return [int(x) for x in raw]
    if isinstance(raw, str):
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    return list(default)


def get_str_list(name: str, env_var: str | None, default: list[str]) -> list[str]:
    """Resolve a list of strings. YAML accepts [a,b]; env accepts 'a,b'."""
    raw = _lookup(name, env_var, default)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return list(default)
