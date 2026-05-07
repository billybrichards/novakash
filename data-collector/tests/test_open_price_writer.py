"""
Tests for audit #395 — market_data.open_price writer fix.

Covers:
 1. fetch_current_markets() reads eventMetadata.priceToBeat
 2. upsert_market() COALESCE semantics: non-NULL open_price not clobbered by NULL
 3. resolve_window() accepts and writes open_price with COALESCE semantics
 4. fetch_resolved_market() returns open_price from eventMetadata
"""

import json
import sys
import types
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so collector.py can be imported without asyncpg / aiohttp
# ---------------------------------------------------------------------------

def _make_stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


if "asyncpg" not in sys.modules:
    asyncpg_mod = _make_stub_module("asyncpg")
    asyncpg_mod.Pool = object  # type: ignore[attr-defined]

if "aiohttp" not in sys.modules:
    aiohttp_mod = _make_stub_module("aiohttp")
    aiohttp_mod.ClientSession = object  # type: ignore[attr-defined]
    aiohttp_mod.ClientTimeout = MagicMock(return_value=None)

if "structlog" not in sys.modules:
    structlog_mod = _make_stub_module("structlog")
    structlog_mod.get_logger = lambda: MagicMock()

import importlib
import os

os.environ.setdefault("DATABASE_URL", "postgresql://fake/fake")

# Import the module under test (import after stubs)
collector = importlib.import_module("collector")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aiohttp_response(json_data, status: int = 200):
    """Return an async context-manager mock that yields a fake aiohttp response."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_event(price_to_beat=None, markets=None, start_date=None):
    """Build a minimal Gamma event payload."""
    event_meta: dict = {}
    if price_to_beat is not None:
        event_meta["priceToBeat"] = price_to_beat

    market_slug = "btc-updown-5m-1775379300"
    default_market = {
        "slug": market_slug,
        "conditionId": "0xABC",
        "question": "BTC up?",
        "outcomePrices": json.dumps(["0.55", "0.45"]),
        "outcomes": json.dumps(["UP", "DOWN"]),
        "clobTokenIds": json.dumps(["token_up", "token_dn"]),
        "bestBid": "0.54",
        "bestAsk": "0.56",
        "volume": "1000",
        "liquidity": "5000",
        "endDate": "2099-01-01T00:05:00Z",
    }
    return {
        "eventMetadata": event_meta,
        "markets": markets if markets is not None else [default_market],
        "startDate": start_date or "2099-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Test: fetch_current_markets reads priceToBeat
# ---------------------------------------------------------------------------

class TestFetchCurrentMarketsOpenPrice(unittest.IsolatedAsyncioTestCase):

    async def test_price_to_beat_captured_when_present(self):
        """open_price in result equals eventMetadata.priceToBeat."""
        event = _make_event(price_to_beat="94300.50")
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response([event])

        with patch.object(collector._limiter, "wait", AsyncMock()):
            results = await collector.fetch_current_markets(mock_session, "BTC", "5m")

        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["open_price"], 94300.50)

    async def test_open_price_none_when_ptb_absent(self):
        """open_price is None when eventMetadata has no priceToBeat."""
        event = _make_event(price_to_beat=None)
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response([event])

        with patch.object(collector._limiter, "wait", AsyncMock()):
            results = await collector.fetch_current_markets(mock_session, "BTC", "5m")

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["open_price"])

    async def test_open_price_none_when_ptb_zero(self):
        """open_price is None when priceToBeat is 0 (invalid sentinel)."""
        event = _make_event(price_to_beat="0")
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response([event])

        with patch.object(collector._limiter, "wait", AsyncMock()):
            results = await collector.fetch_current_markets(mock_session, "BTC", "5m")

        self.assertIsNone(results[0]["open_price"])

    async def test_open_price_none_when_ptb_invalid_string(self):
        """open_price is None when priceToBeat is not numeric."""
        event = _make_event(price_to_beat="not-a-number")
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response([event])

        with patch.object(collector._limiter, "wait", AsyncMock()):
            results = await collector.fetch_current_markets(mock_session, "BTC", "5m")

        self.assertIsNone(results[0]["open_price"])

    async def test_open_price_none_when_event_meta_missing(self):
        """open_price is None when eventMetadata key is entirely absent."""
        event = _make_event()
        del event["eventMetadata"]
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response([event])

        with patch.object(collector._limiter, "wait", AsyncMock()):
            results = await collector.fetch_current_markets(mock_session, "BTC", "5m")

        self.assertIsNone(results[0]["open_price"])

    async def test_open_price_numeric_string_parsed(self):
        """open_price parses correctly from a string-encoded float."""
        event = _make_event(price_to_beat="95000")
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response([event])

        with patch.object(collector._limiter, "wait", AsyncMock()):
            results = await collector.fetch_current_markets(mock_session, "BTC", "5m")

        self.assertAlmostEqual(results[0]["open_price"], 95000.0)


# ---------------------------------------------------------------------------
# Test: upsert_market COALESCE semantics
# ---------------------------------------------------------------------------

class TestUpsertMarketCoalesceOpenPrice(unittest.IsolatedAsyncioTestCase):
    """
    We can't run real SQL, so we verify that the SQL string emitted to asyncpg
    contains the COALESCE expression that protects open_price from NULL clobber.
    """

    async def _capture_execute_sql(self, data: dict) -> str:
        """Run upsert_market with a mock pool and return the SQL sent."""
        captured_sql = []

        mock_conn = AsyncMock()
        async def fake_execute(sql, *args):
            captured_sql.append(sql)
        mock_conn.execute.side_effect = fake_execute

        mock_pool = MagicMock()
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = acquire_ctx

        await collector.upsert_market(mock_pool, data)
        return captured_sql[0] if captured_sql else ""

    async def test_coalesce_present_in_sql(self):
        """upsert SQL must include COALESCE(EXCLUDED.open_price, market_data.open_price)."""
        data = {
            "window_ts": 1775379300, "asset": "BTC", "timeframe": "5m",
            "open_price": 94000.0,
        }
        sql = await self._capture_execute_sql(data)
        self.assertIn("COALESCE(EXCLUDED.open_price, market_data.open_price)", sql)

    async def test_null_open_price_does_not_clobber_existing(self):
        """When open_price=None is supplied, COALESCE preserves the stored value.

        We verify the SQL and parameter semantics: the 16th positional arg ($16)
        is open_price and the COALESCE picks the existing row value if NULL.
        """
        captured_args: list = []

        mock_conn = AsyncMock()
        async def fake_execute(sql, *args):
            captured_args.extend(args)
        mock_conn.execute.side_effect = fake_execute

        mock_pool = MagicMock()
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = acquire_ctx

        data = {
            "window_ts": 1775379300, "asset": "BTC", "timeframe": "5m",
            "open_price": None,  # NULL should not overwrite existing DB value
        }
        await collector.upsert_market(mock_pool, data)

        # The 16th positional parameter ($16) passed to asyncpg is open_price.
        # When None, COALESCE in SQL keeps the existing market_data.open_price.
        self.assertIsNone(captured_args[15])  # 0-indexed: arg 16 = index 15

    async def test_non_null_open_price_propagated(self):
        """When open_price has a real value it is passed as the positional arg."""
        captured_args: list = []

        mock_conn = AsyncMock()
        async def fake_execute(sql, *args):
            captured_args.extend(args)
        mock_conn.execute.side_effect = fake_execute

        mock_pool = MagicMock()
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = acquire_ctx

        data = {
            "window_ts": 1775379300, "asset": "BTC", "timeframe": "5m",
            "open_price": 94321.50,
        }
        await collector.upsert_market(mock_pool, data)
        self.assertAlmostEqual(captured_args[15], 94321.50)


# ---------------------------------------------------------------------------
# Test: resolve_window accepts and writes open_price
# ---------------------------------------------------------------------------

class TestResolveWindowOpenPrice(unittest.IsolatedAsyncioTestCase):

    async def _call_resolve(self, open_price: Optional[float]):
        """Helper: call resolve_window and capture SQL + args."""
        captured: list = []

        mock_conn = AsyncMock()
        async def fake_execute(sql, *args):
            captured.append((sql, args))
        mock_conn.execute.side_effect = fake_execute

        mock_pool = MagicMock()
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = acquire_ctx

        await collector.resolve_window(
            pool=mock_pool,
            window_ts=1775379300,
            asset="BTC",
            timeframe="5m",
            close_price=1.0,
            outcome="UP",
            open_price=open_price,
        )
        return captured[0] if captured else ("", ())

    async def test_coalesce_present_in_resolve_sql(self):
        """resolve_window SQL must include COALESCE(market_data.open_price, $6)."""
        sql, _ = await self._call_resolve(open_price=94500.0)
        self.assertIn("COALESCE(market_data.open_price, $6)", sql)

    async def test_open_price_passed_as_sixth_arg(self):
        """open_price value is passed as the 6th positional arg to asyncpg."""
        _, args = await self._call_resolve(open_price=93750.0)
        # args: (close_price, outcome, window_ts, asset, timeframe, open_price)
        self.assertAlmostEqual(args[5], 93750.0)

    async def test_none_open_price_passed_as_sixth_arg(self):
        """None open_price is accepted without error; COALESCE keeps existing."""
        _, args = await self._call_resolve(open_price=None)
        self.assertIsNone(args[5])

    async def test_default_open_price_is_none(self):
        """resolve_window can be called without open_price (backward-compat)."""
        captured: list = []

        mock_conn = AsyncMock()
        async def fake_execute(sql, *args):
            captured.append(args)
        mock_conn.execute.side_effect = fake_execute

        mock_pool = MagicMock()
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = acquire_ctx

        # Old call-site — no open_price kwarg
        await collector.resolve_window(
            pool=mock_pool,
            window_ts=1775379300,
            asset="BTC",
            timeframe="5m",
            close_price=1.0,
            outcome="DOWN",
        )
        self.assertIsNone(captured[0][5])


# ---------------------------------------------------------------------------
# Test: fetch_resolved_market returns open_price
# ---------------------------------------------------------------------------

class TestFetchResolvedMarketOpenPrice(unittest.IsolatedAsyncioTestCase):

    def _resolved_event(self, price_to_beat=None, final_price=None):
        event_meta: dict = {}
        if price_to_beat is not None:
            event_meta["priceToBeat"] = price_to_beat
        if final_price is not None:
            event_meta["finalPrice"] = final_price
        return [{
            "eventMetadata": event_meta,
            "markets": [{
                "closed": True,
                "outcomePrices": json.dumps(["0.99", "0.01"]),
            }],
        }]

    async def _fetch(self, payload) -> Optional[dict]:
        mock_session = MagicMock()
        mock_session.get.return_value = _make_aiohttp_response(payload)
        with patch.object(collector._limiter, "wait", AsyncMock()):
            return await collector.fetch_resolved_market(mock_session, "btc-updown-5m-1775379300")

    async def test_open_price_from_price_to_beat(self):
        result = await self._fetch(self._resolved_event(price_to_beat="94000"))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["open_price"], 94000.0)

    async def test_no_fallback_to_final_price_when_ptb_absent(self):
        """Review fix 395-3: finalPrice is the window CLOSE price; using it
        as a fallback for open_price silently writes close-as-open. When
        priceToBeat is absent from eventMetadata we keep open_price = None.
        """
        result = await self._fetch(self._resolved_event(final_price="93500.75"))
        self.assertIsNotNone(result)
        self.assertIsNone(result["open_price"])

    async def test_price_to_beat_used_when_both_present(self):
        """priceToBeat is canonical when present (finalPrice is ignored)."""
        result = await self._fetch(
            self._resolved_event(price_to_beat="94000", final_price="90000")
        )
        self.assertAlmostEqual(result["open_price"], 94000.0)

    async def test_open_price_none_when_no_metadata(self):
        """open_price is None when eventMetadata has no relevant keys."""
        result = await self._fetch(self._resolved_event())
        self.assertIsNotNone(result)
        self.assertIsNone(result["open_price"])

    async def test_open_price_key_present_in_result(self):
        """Result dict always includes open_price key (may be None)."""
        result = await self._fetch(self._resolved_event())
        self.assertIn("open_price", result)

    async def test_outcome_unaffected_by_open_price_change(self):
        """Existing outcome logic unchanged after the audit #395 patch."""
        result = await self._fetch(self._resolved_event(price_to_beat="94000"))
        self.assertEqual(result["outcome"], "UP")  # prices[0]=0.99 > 0.5
        self.assertAlmostEqual(result["up_final"], 0.99)


if __name__ == "__main__":
    unittest.main()
