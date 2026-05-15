"""Regression tests for fok_ladder.execute() — CLOB /book 404 race.

Context
-------
Audit 2026-04-26: Engine PID 639498 hammered v9_lgb_only FAKs that all
returned ``book_error: PolyApiException[status_code=404, error_message=
{'error': 'No orderbook exists for the requested token id'}]``.

Reproduction (curl from Montreal):
    Markets at or past their ``endDate`` return Gamma ``closed=False`` but
    CLOB returns 404 No orderbook. Freshly-published markets briefly
    exhibit the same race on the leading edge before CLOB indexes the
    new book.

Fix
---
fok_ladder.execute() now retries the book fetch once after a short
delay (handles propagation lag). If still 404, returns a
``book_unavailable_404`` skip — distinct from generic ``book_error``
so the caller can reason about it. The FAK-ladder executor recognises
the prefix and short-circuits Phase 2/3 (RFQ + GTC would also 404).

What this test covers
---------------------
* 404 on first attempt + success on retry → ladder proceeds normally.
* 404 on both attempts → returns FOKResult with
  ``abort_reason='book_unavailable_404…'`` and zero attempted prices.
* Non-404 exception → no retry, legacy ``book_error: <msg>`` path.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from execution.fok_ladder import FOKLadder


class _FakePolyApiException(Exception):
    """Stand-in for py_clob_client.exceptions.PolyApiException.

    Mirrors the SDK shape: ``.status_code`` attribute + a ``__repr__`` /
    ``__str__`` containing ``status_code=404`` and the orderbook payload.
    Avoids importing the real SDK so this test runs anywhere.
    """

    def __init__(self, status_code: int, error_msg: str) -> None:
        self.status_code = status_code
        self.error_msg = error_msg

    def __repr__(self) -> str:
        return (
            f"PolyApiException[status_code={self.status_code}, "
            f"error_message={self.error_msg}]"
        )

    def __str__(self) -> str:
        return self.__repr__()


def _book_404() -> _FakePolyApiException:
    return _FakePolyApiException(
        status_code=404,
        error_msg={"error": "No orderbook exists for the requested token id"},
    )


@pytest.mark.asyncio
async def test_book_404_retry_then_success(monkeypatch):
    """First /book call 404s, retry succeeds — ladder runs normally."""
    poly = AsyncMock()
    # First raise, then return a usable best-ask
    poly.get_clob_best_ask = AsyncMock(side_effect=[_book_404(), 0.55])
    # FAK call: zero fill at cap, zero fill at cap+pi → exhausted (we
    # don't care about the fill — we care that retry recovered + ladder
    # advanced past the book check).
    poly.place_market_order = AsyncMock(
        return_value={"size_matched": 0, "order_id": None, "filled": False}
    )

    ladder = FOKLadder(poly)
    # Skip the real 1.5s sleep
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", AsyncMock())

    result = await ladder.execute(
        token_id="x" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.65, min_price=0.30,
    )

    # get_clob_best_ask called exactly twice (initial + 1 retry)
    assert poly.get_clob_best_ask.await_count == 2
    # Ladder proceeded to actually attempt FAKs (default 4-rung ladder
    # post-2026-05-14: [cap, cap+0.02, cap+0.04, cap+0.07] up to $0.92)
    assert poly.place_market_order.await_count == 4
    # Final result: exhausted (no fills) but NOT a book_unavailable abort
    assert result.filled is False
    assert (result.abort_reason or "").startswith("book_unavailable") is False
    assert len(result.attempted_prices) == 4


@pytest.mark.asyncio
async def test_book_404_persistent_returns_book_unavailable(monkeypatch):
    """Both attempts 404 — clean skip, no FAK attempts, distinct reason."""
    poly = AsyncMock()
    poly.get_clob_best_ask = AsyncMock(side_effect=[_book_404(), _book_404()])
    poly.place_market_order = AsyncMock()

    ladder = FOKLadder(poly)
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", AsyncMock())

    result = await ladder.execute(
        token_id="y" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.65, min_price=0.30,
    )

    assert poly.get_clob_best_ask.await_count == 2
    # Critically — never fired a FAK on a missing orderbook
    poly.place_market_order.assert_not_awaited()

    assert result.filled is False
    assert result.attempted_prices == []
    assert result.abort_reason is not None
    assert result.abort_reason.startswith("book_unavailable_404")


@pytest.mark.asyncio
async def test_book_non_404_exception_no_retry(monkeypatch):
    """Non-404 errors (e.g. empty book ValueError) keep the legacy path —
    no retry, no ``book_unavailable_404`` reason, plain ``book_error:``."""
    poly = AsyncMock()
    poly.get_clob_best_ask = AsyncMock(
        side_effect=ValueError("No ask-side liquidity for token abc...")
    )
    poly.place_market_order = AsyncMock()

    ladder = FOKLadder(poly)
    sleep_mock = AsyncMock()
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", sleep_mock)

    result = await ladder.execute(
        token_id="z" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.65, min_price=0.30,
    )

    # No retry — single attempt, no sleep
    assert poly.get_clob_best_ask.await_count == 1
    sleep_mock.assert_not_awaited()
    poly.place_market_order.assert_not_awaited()

    assert result.filled is False
    assert (result.abort_reason or "").startswith("book_error:")
    # Specifically NOT the new 404 reason
    assert "book_unavailable_404" not in (result.abort_reason or "")


@pytest.mark.asyncio
async def test_is_book_404_detection_by_str_match():
    """Defensive: detect 404 even when status_code attribute isn't surfaced
    (e.g. exception wrapped in a runtime layer that drops the attr but
    keeps the repr)."""

    class WrappedException(Exception):
        # No status_code attribute — only the stringified payload
        def __str__(self) -> str:
            return (
                "PolyApiException[status_code=404, "
                "error_message={'error': 'No orderbook exists for the requested token id'}]"
            )

    assert FOKLadder._is_book_404(WrappedException()) is True

    # Negative case: a 500 with same shape must NOT be classified as 404
    class WrappedException500(Exception):
        def __str__(self) -> str:
            return "PolyApiException[status_code=500, error_message='internal']"

    assert FOKLadder._is_book_404(WrappedException500()) is False


# ──────────────────────────────────────────────────────────────────────────
# 4-rung FAK ladder tests (2026-05-14)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_four_rung_ladder_default_prices(monkeypatch):
    """Default 4-rung ladder (cap=0.85) produces [0.85, 0.87, 0.89, 0.92]."""
    monkeypatch.delenv("FAK_LADDER_RUNGS", raising=False)
    monkeypatch.delenv("FAK_LADDER_MAX_PRICE", raising=False)

    poly = AsyncMock()
    poly.get_clob_best_ask = AsyncMock(return_value=0.50)
    poly.place_market_order = AsyncMock(
        return_value={"size_matched": 0, "order_id": None, "filled": False}
    )

    ladder = FOKLadder(poly)
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", AsyncMock())

    result = await ladder.execute(
        token_id="a" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.85, min_price=0.30,
    )

    assert result.filled is False
    assert result.attempted_prices == [0.85, 0.87, 0.89, 0.92]
    assert poly.place_market_order.await_count == 4


@pytest.mark.asyncio
async def test_four_rung_ladder_ceiling_caps(monkeypatch):
    """cap=0.90 + default deltas → rungs above 0.92 dropped → [0.90, 0.92]."""
    monkeypatch.delenv("FAK_LADDER_RUNGS", raising=False)
    monkeypatch.delenv("FAK_LADDER_MAX_PRICE", raising=False)

    poly = AsyncMock()
    poly.get_clob_best_ask = AsyncMock(return_value=0.50)
    poly.place_market_order = AsyncMock(
        return_value={"size_matched": 0, "order_id": None, "filled": False}
    )

    ladder = FOKLadder(poly)
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", AsyncMock())

    result = await ladder.execute(
        token_id="b" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.90, min_price=0.30,
    )

    # 0.90, 0.92 in; 0.94 + 0.97 dropped (> 0.92 ceiling)
    assert result.attempted_prices == [0.90, 0.92]


@pytest.mark.asyncio
async def test_malformed_env_falls_back_to_legacy(monkeypatch):
    """FAK_LADDER_RUNGS malformed → falls back to legacy [cap, cap+pi]."""
    monkeypatch.setenv("FAK_LADDER_RUNGS", "not_a_number,0.02")

    poly = AsyncMock()
    poly.get_clob_best_ask = AsyncMock(return_value=0.50)
    poly.place_market_order = AsyncMock(
        return_value={"size_matched": 0, "order_id": None, "filled": False}
    )

    ladder = FOKLadder(poly)
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", AsyncMock())

    result = await ladder.execute(
        token_id="c" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.65, min_price=0.30,
    )

    # Legacy 2-rung: [cap=0.65, cap+0.0314 rounded to 0.68]
    assert result.attempted_prices == [0.65, 0.68]
    assert poly.place_market_order.await_count == 2


@pytest.mark.asyncio
async def test_custom_rungs_via_env(monkeypatch):
    """FAK_LADDER_RUNGS env override works."""
    monkeypatch.setenv("FAK_LADDER_RUNGS", "0.0,0.03,0.06")
    monkeypatch.delenv("FAK_LADDER_MAX_PRICE", raising=False)

    poly = AsyncMock()
    poly.get_clob_best_ask = AsyncMock(return_value=0.50)
    poly.place_market_order = AsyncMock(
        return_value={"size_matched": 0, "order_id": None, "filled": False}
    )

    ladder = FOKLadder(poly)
    monkeypatch.setattr("execution.fok_ladder.asyncio.sleep", AsyncMock())

    result = await ladder.execute(
        token_id="d" * 32, direction="BUY", stake_usd=5.0,
        max_price=0.85, min_price=0.30,
    )

    assert result.attempted_prices == [0.85, 0.88, 0.91]
    assert poly.place_market_order.await_count == 3
