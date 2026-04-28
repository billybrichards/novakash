"""
Compat shim for py-clob-client-v2 return-type changes.

v1's ``ClobClient.get_order_book(token)`` returned an ``OrderBookSummary``
dataclass with ``.bids`` / ``.asks`` as lists of ``OrderSummary`` objects
(``.price`` / ``.size`` attributes).

v2 returns the raw HTTP dict — keys are strings, ``bids`` / ``asks`` are
``list[dict]`` of ``{"price": str, "size": str}``. Every call site in the
engine that does ``book.bids[0].price`` therefore breaks with
``'dict' object has no attribute 'bids'``.

The v2 SDK already ships ``utilities.parse_raw_orderbook_summary`` which
turns the dict into the same ``OrderBookSummary`` dataclass v1 returned.
We wrap ``get_order_book`` at import time so callers never see the dict.

Apply at engine boot, BEFORE any ClobClient.get_order_book() call. Idempotent.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def _patch_get_order_book() -> None:
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.utilities import parse_raw_orderbook_summary
    except ImportError:
        _log.warning("py_clob_client_v2 not installed; get_order_book patch skipped")
        return

    if getattr(ClobClient.get_order_book, "_polymarket_patched", False):
        return

    _orig = ClobClient.get_order_book

    def _patched(self, token_id):
        raw = _orig(self, token_id)
        if isinstance(raw, dict):
            try:
                return parse_raw_orderbook_summary(raw)
            except Exception:
                # If the dict is malformed (e.g. error response), let the
                # caller handle the original payload — we don't want to
                # turn a Polymarket error into an obscure parse exception.
                return raw
        return raw

    _patched._polymarket_patched = True
    ClobClient.get_order_book = _patched
    _log.warning("py_clob_client_v2 ClobClient.get_order_book wrapped: dict -> OrderBookSummary")


def apply_patch() -> None:
    try:
        _patch_get_order_book()
    except Exception as exc:  # noqa: BLE001
        _log.warning("get_order_book patch failed: %s", exc)


apply_patch()
