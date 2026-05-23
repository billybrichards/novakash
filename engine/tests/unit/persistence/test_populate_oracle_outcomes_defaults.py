"""Regression: defaults for PgWindowRepository.populate_oracle_outcomes.

Audit RDS #614 (2026-05-23): the prior 15-min ``lookback_seconds``
default meant any window that missed a single Gamma sweep — engine
restart, transient 5xx, Cloudflare hiccup — was permanently skipped
because the next sweep didn't go far enough back to retry it.

These tests pin the new defaults so a future refactor can't silently
reintroduce the bug:

  * ``lookback_seconds`` must default to at least 6h (21600s).
  * ``min_age_seconds`` must default to at least 6 min (oracle settle lag).

Both the port spec and the asyncpg adapter are checked.
"""

from __future__ import annotations

import inspect

from adapters.persistence.pg_window_repo import PgWindowRepository
from domain.ports import WindowStateRepository


def _get_default(callable_, name: str):
    sig = inspect.signature(callable_)
    return sig.parameters[name].default


def test_pg_window_repo_lookback_default_at_least_6h():
    """Adapter default lookback must cover several hours of backlog."""
    default = _get_default(
        PgWindowRepository.populate_oracle_outcomes, "lookback_seconds"
    )
    assert default >= 6 * 3600, (
        f"populate_oracle_outcomes(lookback_seconds=) default is {default}s, "
        "must be >= 6h or transient Gamma failures will leak windows "
        "permanently (audit RDS #614)."
    )


def test_pg_window_repo_min_age_default_at_least_six_minutes():
    default = _get_default(
        PgWindowRepository.populate_oracle_outcomes, "min_age_seconds"
    )
    assert default >= 360, (
        f"populate_oracle_outcomes(min_age_seconds=) default is {default}s, "
        "must be >= 360s so Gamma has had time to publish resolution."
    )


def test_port_lookback_default_matches_adapter():
    """Port and adapter defaults must agree — drift here would silently
    fall back to the old 15-min behaviour for any caller that relied on
    the port's signature."""
    port_default = _get_default(
        WindowStateRepository.populate_oracle_outcomes, "lookback_seconds"
    )
    adapter_default = _get_default(
        PgWindowRepository.populate_oracle_outcomes, "lookback_seconds"
    )
    assert port_default == adapter_default, (
        f"port default ({port_default}) != adapter default "
        f"({adapter_default})"
    )
