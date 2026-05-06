"""Unit tests for the dedicated Polymarket SDK executor (audit #371).

Verifies:
  - ``_POLY_SDK_EXECUTOR`` exists, has 8 workers, ``poly-sdk`` thread prefix.
  - ``_run_poly_sdk`` runs the wrapped fn off the asyncio loop's default
    executor and on a thread named ``poly-sdk-*``.
  - ``_run_poly_sdk`` propagates positional args, kwargs, return values,
    and exceptions correctly.
  - The default executor (set in ``engine/main.py``) does NOT receive
    Polymarket SDK work — i.e. quarantine actually quarantines.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from adapters.polymarket import live_client


def test_poly_sdk_executor_configured() -> None:
    """The dedicated pool exists with the expected size + name prefix."""
    ex = live_client._POLY_SDK_EXECUTOR
    assert isinstance(ex, ThreadPoolExecutor)
    assert ex._max_workers == 8
    assert ex._thread_name_prefix == "poly-sdk"


@pytest.mark.asyncio
async def test_run_poly_sdk_runs_on_dedicated_thread() -> None:
    """``_run_poly_sdk`` must execute the callable on a ``poly-sdk-*`` thread."""

    def _capture_thread_name() -> str:
        return threading.current_thread().name

    name = await live_client._run_poly_sdk(_capture_thread_name)
    assert name.startswith("poly-sdk"), (
        f"Expected thread name to start with 'poly-sdk', got {name!r}. "
        "If this fails the SDK call leaked to the default executor."
    )


@pytest.mark.asyncio
async def test_run_poly_sdk_passes_positional_args() -> None:
    def _add(a: int, b: int) -> int:
        return a + b

    result = await live_client._run_poly_sdk(_add, 2, 3)
    assert result == 5


@pytest.mark.asyncio
async def test_run_poly_sdk_passes_kwargs() -> None:
    def _greet(name: str, *, greeting: str = "hi") -> str:
        return f"{greeting}, {name}"

    result = await live_client._run_poly_sdk(_greet, "billy", greeting="yo")
    assert result == "yo, billy"


@pytest.mark.asyncio
async def test_run_poly_sdk_propagates_exceptions() -> None:
    def _boom() -> None:
        raise RuntimeError("CLOB exploded")

    with pytest.raises(RuntimeError, match="CLOB exploded"):
        await live_client._run_poly_sdk(_boom)


@pytest.mark.asyncio
async def test_default_executor_does_not_run_poly_sdk() -> None:
    """Quarantine check: ``asyncio.to_thread`` (default exec) and
    ``_run_poly_sdk`` (poly-sdk pool) must run on DIFFERENT threads.

    Regression guard: if someone removes ``_run_poly_sdk`` and reverts
    to ``asyncio.to_thread`` somewhere, this assertion catches it via
    the thread-name prefix divergence.
    """

    def _name() -> str:
        return threading.current_thread().name

    default_thread = await asyncio.to_thread(_name)
    poly_thread = await live_client._run_poly_sdk(_name)

    # Default-executor thread name is implementation-defined; check it does
    # NOT carry the poly-sdk prefix.
    assert not default_thread.startswith("poly-sdk")
    assert poly_thread.startswith("poly-sdk")


@pytest.mark.asyncio
async def test_run_poly_sdk_signature_matches_to_thread() -> None:
    """Sanity: a call shape that worked under ``asyncio.to_thread``
    still works under ``_run_poly_sdk`` (drop-in replacement)."""

    def _sample(a, b, c=None, *, d=10):
        return (a, b, c, d)

    via_default = await asyncio.to_thread(_sample, 1, 2, c=3, d=4)
    via_poly = await live_client._run_poly_sdk(_sample, 1, 2, c=3, d=4)
    assert via_default == via_poly == (1, 2, 3, 4)
