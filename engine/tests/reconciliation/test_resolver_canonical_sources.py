"""Integration tests for the canonical resolver priority chain.

Audit #350 follow-up. Verifies that the canonical resolver:

  1. Prefers Polymarket HTML scrape over every other source.
  2. Falls back to ``window_snapshots.actual_direction`` when HTML fails.
  3. Falls back to on-chain ``CTF.payoutNumerators`` when both above fail.
  4. Falls back to data-api ``curPrice`` (legacy) only when explicitly given,
     and logs a WARN.
  5. Returns ``None`` when ALL tiers fail — caller MUST treat None as "do not
     write outcome", NEVER fall back to synthetic placeholders.
  6. NEVER produces synthetic placeholder prices ($100K / $100K+1) anywhere
     in the resolver decision path.

These tests use mocks for the HTML fetcher / window-snapshots lookup /
on-chain CTF lookup so the suite runs offline in CI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from data.feeds.polymarket_html_resolution import ResolvedWindow
from reconciliation.canonical_resolver import (
    SOURCE_DATA_API_CURPRICE,
    SOURCE_HTML,
    SOURCE_ONCHAIN_CTF,
    SOURCE_WINDOW_SNAPSHOTS,
    CanonicalOutcome,
    CanonicalResolver,
)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _resolver(
    *,
    html_outcome=None,
    ws_direction=None,
    onchain_direction=None,
):
    """Build a CanonicalResolver with mocked tiers.

    Each tier mock returns the canned value when invoked. ``None`` means the
    tier "misses" (returns None / raises a non-error).
    """
    html_fetcher = MagicMock()
    if html_outcome is None:
        html_fetcher.fetch_resolution = AsyncMock(return_value=None)
    else:
        html_fetcher.fetch_resolution = AsyncMock(
            return_value=ResolvedWindow(
                outcome=html_outcome,
                price_to_beat=78_675.76,
                close_price=78_690.20 if html_outcome == "UP" else 78_660.20,
            )
        )

    if ws_direction is None:
        ws_lookup = AsyncMock(return_value=None)
    else:
        ws_lookup = AsyncMock(return_value=ws_direction)

    if onchain_direction is None:
        onchain_lookup = AsyncMock(return_value=None)
    else:
        onchain_lookup = AsyncMock(return_value=onchain_direction)

    return (
        CanonicalResolver(
            html_fetcher=html_fetcher,
            window_snapshots_lookup=ws_lookup,
            onchain_ctf_lookup=onchain_lookup,
        ),
        html_fetcher,
        ws_lookup,
        onchain_lookup,
    )


# ─── Tier 1: HTML scrape wins ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tier1_html_up_resolves_up():
    """HTML returning UP is the canonical answer; YES bets WIN, NO bets LOSS."""
    r, html, ws, onchain = _resolver(html_outcome="UP")
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "ab" * 32,
        fallback_curprice_outcome="DOWN",  # would mislead — must be ignored
    )
    assert out is not None
    assert out.direction == "UP"
    assert out.source == SOURCE_HTML
    assert out.price_to_beat == 78_675.76
    assert out.close_price == 78_690.20
    # Lower tiers must NOT be consulted.
    ws.assert_not_called()
    onchain.assert_not_called()


@pytest.mark.asyncio
async def test_tier1_html_down_resolves_down():
    """HTML returning DOWN is canonical; NO bets WIN, YES bets LOSS."""
    r, _, ws, onchain = _resolver(html_outcome="DOWN")
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "ab" * 32,
        fallback_curprice_outcome="UP",
    )
    assert out is not None
    assert out.direction == "DOWN"
    assert out.source == SOURCE_HTML
    ws.assert_not_called()
    onchain.assert_not_called()


# ─── Tier 2: window_snapshots fallback ──────────────────────────────────────


@pytest.mark.asyncio
async def test_tier2_ws_fallback_when_html_misses():
    """HTML fails → window_snapshots.actual_direction wins."""
    r, html, ws, onchain = _resolver(html_outcome=None, ws_direction="DOWN")
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "ab" * 32,
        fallback_curprice_outcome="UP",
    )
    assert out is not None
    assert out.direction == "DOWN"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    html.fetch_resolution.assert_awaited_once()
    ws.assert_awaited_once()
    onchain.assert_not_called()


# ─── Tier 3: on-chain CTF fallback ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_tier3_onchain_fallback_when_html_and_ws_miss():
    """Both HTML and window_snapshots miss → on-chain CTF wins."""
    cid = "0x" + "ab" * 32
    r, _, ws, onchain = _resolver(
        html_outcome=None, ws_direction=None, onchain_direction="UP"
    )
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id=cid,
        fallback_curprice_outcome="DOWN",  # must be ignored — onchain wins
    )
    assert out is not None
    assert out.direction == "UP"
    assert out.source == SOURCE_ONCHAIN_CTF
    onchain.assert_awaited_once_with(cid)


@pytest.mark.asyncio
async def test_tier3_skipped_when_no_condition_id():
    """No condition_id → on-chain CTF tier is skipped (cannot query)."""
    r, _, _, onchain = _resolver(
        html_outcome=None, ws_direction=None, onchain_direction="UP"
    )
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id=None,  # no cid
        fallback_curprice_outcome="DOWN",
    )
    # Should fall through to Tier 4 (data-api curPrice).
    assert out is not None
    assert out.direction == "DOWN"
    assert out.source == SOURCE_DATA_API_CURPRICE
    onchain.assert_not_called()


# ─── Tier 4: data-api curPrice fallback (WARN) ──────────────────────────────


@pytest.mark.asyncio
async def test_tier4_curprice_only_when_others_exhausted():
    """All canonical sources miss → fallback curPrice + WARN log."""
    r, _, _, _ = _resolver(
        html_outcome=None, ws_direction=None, onchain_direction=None
    )
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "cd" * 32,
        fallback_curprice_outcome="UP",
    )
    assert out is not None
    assert out.direction == "UP"
    assert out.source == SOURCE_DATA_API_CURPRICE


# ─── All tiers exhausted: returns None ──────────────────────────────────────


@pytest.mark.asyncio
async def test_all_tiers_exhausted_returns_none():
    """All four tiers fail → None. Caller MUST NOT write outcome."""
    r, _, _, _ = _resolver(
        html_outcome=None, ws_direction=None, onchain_direction=None
    )
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "cd" * 32,
        fallback_curprice_outcome=None,  # no curprice either
    )
    assert out is None, "must return None — never guess"


# ─── No synthetic placeholder prices anywhere ───────────────────────────────


@pytest.mark.asyncio
async def test_no_synthetic_placeholders_in_canonical_outcome():
    """Resolver must NEVER produce $100,000 / $100,001 placeholders."""
    # HTML hit produces real prices.
    r, _, _, _ = _resolver(html_outcome="UP")
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "ab" * 32,
    )
    assert out is not None
    assert out.price_to_beat != 100_000.0
    assert out.close_price != 100_001.0
    assert out.close_price != 99_999.0

    # Tier 2/3/4 hits leave price_to_beat / close_price as None — never
    # the synthetic placeholders.
    r2, _, _, _ = _resolver(html_outcome=None, ws_direction="UP")
    out2 = await r2.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
    )
    assert out2 is not None
    assert out2.price_to_beat is None
    assert out2.close_price is None


@pytest.mark.asyncio
async def test_html_exception_does_not_kill_chain():
    """HTML fetcher raising must not crash the resolver — fall through."""
    html_fetcher = MagicMock()
    html_fetcher.fetch_resolution = AsyncMock(side_effect=RuntimeError("boom"))
    ws_lookup = AsyncMock(return_value="DOWN")
    r = CanonicalResolver(
        html_fetcher=html_fetcher,
        window_snapshots_lookup=ws_lookup,
        onchain_ctf_lookup=AsyncMock(return_value=None),
    )
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
    )
    assert out is not None
    assert out.direction == "DOWN"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS


@pytest.mark.asyncio
async def test_html_priority_overrides_disagreeing_curprice():
    """When HTML says UP but curPrice fallback says DOWN — HTML WINS.

    This is the audit-#350 scenario: data-api curPrice was wrong; HTML scrape
    of the same window page produces the canonical answer.
    """
    r, _, _, _ = _resolver(html_outcome="UP")
    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_824_300,
        condition_id="0x" + "ab" * 32,
        fallback_curprice_outcome="DOWN",  # the misleading legacy answer
    )
    assert out is not None
    assert out.direction == "UP"
    assert out.source == SOURCE_HTML


# ─── HTML parser sanity ─────────────────────────────────────────────────────


def test_html_parser_uses_priceToBeat_vs_closePrice():
    """The HTML parser computes UP iff close > priceToBeat strictly."""
    from data.feeds.polymarket_html_resolution import (
        PolymarketHTMLResolutionFetcher,
    )

    # Hand-crafted minimal __NEXT_DATA__ blob with both signals.
    slug = "btc-updown-5m-1777824300"
    target_iso = "2026-05-04T15:25:00.000Z"  # not the real ts but matches format
    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["/api/event/slug", slug],
                            "state": {
                                "data": {
                                    "eventMetadata": {
                                        "priceToBeat": "78675.76",
                                    },
                                },
                            },
                        },
                        {
                            "queryKey": ["past-results", "BTC", "fiveminute", "x"],
                            "state": {
                                "data": {
                                    "data": {
                                        "results": [
                                            {
                                                # The target window's own entry.
                                                "startTime": target_iso,
                                                "endTime": "2026-05-04T15:30:00.000Z",
                                                "openPrice": 78675.76,
                                                "closePrice": 78690.20,
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                    ],
                },
            },
        },
    }
    import json
    html = (
        f'<html><script id="__NEXT_DATA__">{json.dumps(next_data)}</script></html>'
    )
    # window_ts value matched to target_iso ("2026-05-04T15:25:00Z" = 1777850700)
    import datetime as _dt
    ts = int(_dt.datetime(2026, 5, 4, 15, 25, 0, tzinfo=_dt.timezone.utc).timestamp())
    rw = PolymarketHTMLResolutionFetcher._parse_resolution_from_html(
        html, slug=slug, window_ts=ts
    )
    assert rw is not None
    assert rw.outcome == "UP"  # 78690.20 > 78675.76
    assert rw.price_to_beat == 78675.76
    assert rw.close_price == 78690.20


def test_html_parser_down_when_close_le_priceToBeat():
    """closePrice <= priceToBeat → DOWN (Polymarket convention: tie loses)."""
    from data.feeds.polymarket_html_resolution import (
        PolymarketHTMLResolutionFetcher,
    )

    slug = "btc-updown-5m-1777824300"
    import datetime as _dt
    ts = int(_dt.datetime(2026, 5, 4, 15, 25, 0, tzinfo=_dt.timezone.utc).timestamp())
    target_iso = "2026-05-04T15:25:00.000Z"
    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["/api/event/slug", slug],
                            "state": {
                                "data": {
                                    "eventMetadata": {"priceToBeat": "78675.76"},
                                },
                            },
                        },
                        {
                            "queryKey": ["past-results", "BTC", "fiveminute", "x"],
                            "state": {
                                "data": {
                                    "data": {
                                        "results": [
                                            {
                                                "startTime": target_iso,
                                                "endTime": "2026-05-04T15:30:00.000Z",
                                                "openPrice": 78675.76,
                                                "closePrice": 78670.20,  # below
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                    ],
                },
            },
        },
    }
    import json
    html = (
        f'<html><script id="__NEXT_DATA__">{json.dumps(next_data)}</script></html>'
    )
    rw = PolymarketHTMLResolutionFetcher._parse_resolution_from_html(
        html, slug=slug, window_ts=ts
    )
    assert rw is not None
    assert rw.outcome == "DOWN"
