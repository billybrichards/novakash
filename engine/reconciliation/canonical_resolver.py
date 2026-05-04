"""Canonical Polymarket window-outcome resolver — priority chain.

Audit #350 follow-up. Replaces per-writer ad-hoc direction logic in
``order_manager._resolve_from_polymarket``, ``ReconcilePositionsUseCase
.resolve_one``, and ``reconciler._backfill_on_startup`` /
``_resolve_orphaned_fills`` — all of which trusted ``data-api.curPrice`` as a
single point of failure.

The data-api ``curPrice`` field is laggy and can briefly read ``< 0.99`` for
the actual winning side during settlement, or read ``0.0`` after on-chain
redemption clears the position. Either case made every downstream writer
classify a WIN as a LOSS. Audit #350 (trades 7378-7380, 2026-05-04 21:47-22:39)
was the visible symptom; the same code paths could mis-write at any time.

Priority chain (first non-None wins):

  1. **Polymarket HTML scrape** of the same resolved window.
     Source: ``PolymarketHTMLResolutionFetcher`` — parses ``__NEXT_DATA__``
     and compares ``priceToBeat`` to the window's own ``closePrice``. This is
     the SAME data the public Polymarket UI displays and the same Chainlink
     sample the on-chain CTF used.

  2. **window_snapshots.actual_direction** from the engine's own DB.
     Populated by the market-data writer at window close from Chainlink
     open/close — already canonical when present.

  3. **On-chain ``CTF.payoutNumerators(conditionId, index)``**.
     Absolute truth: the conditional-token framework holds the post-resolution
     payout vector. ``payoutNumerators(_, 0)`` for the YES/UP slot is 1 if UP
     won, 0 if DOWN won. Slow (RPC round-trip) so used only after HTML+DB miss.
     Same source ``scripts/ops/wallet_truth.py`` and ``check_pending.py`` use.

  4. **data-api ``curPrice``** — the legacy path. Logged at WARN whenever it
     fires so we can monitor frequency.

If all four miss, the helper returns ``None``. Callers MUST treat None as
"do not write outcome yet" — never fall back to synthetic placeholders.

Public surface
--------------
* ``CanonicalResolver`` — instantiated once at engine startup, reused.
* ``CanonicalResolver.resolve_window_outcome_canonical(asset, timeframe,
  window_ts, *, condition_id=None, fallback_curprice_outcome=None) ->
  Optional[CanonicalOutcome]``
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import structlog

from data.feeds.polymarket_html_resolution import (
    PolymarketHTMLResolutionFetcher,
    ResolvedWindow,
)

log = structlog.get_logger(__name__)


# Canonical outcome source identifiers (ordered by priority).
SOURCE_HTML = "polymarket_html"
SOURCE_WINDOW_SNAPSHOTS = "window_snapshots"
SOURCE_ONCHAIN_CTF = "onchain_ctf"
SOURCE_DATA_API_CURPRICE = "data_api_curprice"


@dataclass(frozen=True)
class CanonicalOutcome:
    """Resolved window outcome with provenance.

    Attributes:
        direction: ``"UP"`` or ``"DOWN"``.
        source: Which tier produced this — one of ``SOURCE_*`` constants.
        price_to_beat: Optional priceToBeat (only set when source=HTML).
        close_price: Optional Chainlink closePrice (only set when source=HTML).
    """

    direction: str
    source: str
    price_to_beat: Optional[float] = None
    close_price: Optional[float] = None


# Type alias for the async function the helper uses to hit window_snapshots.
# Keeps the helper decoupled from any specific repo implementation. The
# function should return ``"UP"`` / ``"DOWN"`` / ``None``.
WindowSnapshotsLookup = Callable[[str, int], Awaitable[Optional[str]]]

# Type alias for the on-chain CTF payoutNumerators lookup. The function
# accepts the position's condition_id (hex) and returns the winning slot
# direction (``"UP"`` for YES-slot=1, ``"DOWN"`` for NO-slot=1) or ``None``
# if the market hasn't resolved on-chain yet.
OnchainCTFLookup = Callable[[str], Awaitable[Optional[str]]]


class CanonicalResolver:
    """Run the canonical priority chain for window resolution outcome.

    Constructed once at engine startup. Holds the HTML fetcher (which has its
    own caches) and references to the window-snapshots and on-chain CTF
    lookup callables provided by the caller — keeps the helper free of direct
    DB / Web3 dependencies for testability.
    """

    def __init__(
        self,
        html_fetcher: Optional[PolymarketHTMLResolutionFetcher] = None,
        window_snapshots_lookup: Optional[WindowSnapshotsLookup] = None,
        onchain_ctf_lookup: Optional[OnchainCTFLookup] = None,
    ) -> None:
        self._html_fetcher = html_fetcher or PolymarketHTMLResolutionFetcher()
        self._window_snapshots_lookup = window_snapshots_lookup
        self._onchain_ctf_lookup = onchain_ctf_lookup
        self._log = log.bind(component="CanonicalResolver")

    async def resolve_window_outcome_canonical(
        self,
        asset: str,
        timeframe: str,
        window_ts: int,
        *,
        condition_id: Optional[str] = None,
        fallback_curprice_outcome: Optional[str] = None,
    ) -> Optional[CanonicalOutcome]:
        """Run the priority chain for canonical UP/DOWN outcome.

        Args:
            asset: ``"BTC"``, ``"ETH"``, etc.
            timeframe: ``"5m"`` or ``"15m"``.
            window_ts: Window-aligned epoch seconds.
            condition_id: Optional Polymarket condition_id (hex with ``0x``).
                Required for the on-chain CTF tier. Pass ``None`` to skip that
                tier (e.g. when the calling site doesn't have it handy).
            fallback_curprice_outcome: Optional pre-computed
                ``"UP"`` / ``"DOWN"`` from the data-api ``curPrice`` path
                (whatever the legacy code already decided). Used as Tier 4.
                Pass ``None`` if not available — the helper will return
                ``None`` rather than guess.

        Returns:
            ``CanonicalOutcome`` with provenance, or ``None`` if every tier
            failed. ``None`` MUST be treated as "do not write outcome";
            callers must NOT fall back to synthetic placeholder prices.
        """
        asset_u = asset.upper()
        tf = timeframe.lower()

        # ── Tier 1: Polymarket HTML scrape ────────────────────────────────
        try:
            rw = await self._html_fetcher.fetch_resolution(
                asset_u, tf, int(window_ts)
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            self._log.warning(
                "canonical_resolver.html_unexpected_error",
                asset=asset_u, timeframe=tf, window_ts=window_ts,
                error=str(exc)[:200],
            )
            rw = None

        if rw is not None:
            self._log.info(
                "canonical_resolver.html_hit",
                asset=asset_u, timeframe=tf, window_ts=window_ts,
                outcome=rw.outcome,
                price_to_beat=rw.price_to_beat,
                close_price=rw.close_price,
            )
            return CanonicalOutcome(
                direction=rw.outcome,
                source=SOURCE_HTML,
                price_to_beat=rw.price_to_beat,
                close_price=rw.close_price,
            )

        # ── Tier 2: window_snapshots.actual_direction ─────────────────────
        if self._window_snapshots_lookup is not None:
            try:
                direction = await self._window_snapshots_lookup(
                    asset_u, int(window_ts)
                )
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "canonical_resolver.window_snapshots_error",
                    asset=asset_u, window_ts=window_ts, error=str(exc)[:200],
                )
                direction = None
            if direction in ("UP", "DOWN"):
                self._log.info(
                    "canonical_resolver.window_snapshots_hit",
                    asset=asset_u, window_ts=window_ts, direction=direction,
                )
                return CanonicalOutcome(
                    direction=direction,
                    source=SOURCE_WINDOW_SNAPSHOTS,
                )

        # ── Tier 3: on-chain CTF.payoutNumerators ─────────────────────────
        if self._onchain_ctf_lookup is not None and condition_id:
            try:
                direction = await self._onchain_ctf_lookup(condition_id)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "canonical_resolver.onchain_ctf_error",
                    condition_id=condition_id[:20], error=str(exc)[:200],
                )
                direction = None
            if direction in ("UP", "DOWN"):
                self._log.info(
                    "canonical_resolver.onchain_ctf_hit",
                    asset=asset_u, window_ts=window_ts,
                    condition_id=condition_id[:20], direction=direction,
                )
                return CanonicalOutcome(
                    direction=direction,
                    source=SOURCE_ONCHAIN_CTF,
                )

        # ── Tier 4: data-api curPrice fallback (logged at WARN) ───────────
        if fallback_curprice_outcome in ("UP", "DOWN"):
            self._log.warning(
                "canonical_resolver.data_api_curprice_fallback",
                asset=asset_u, window_ts=window_ts,
                direction=fallback_curprice_outcome,
                note="canonical sources unavailable; using legacy data-api curPrice",
            )
            return CanonicalOutcome(
                direction=fallback_curprice_outcome,
                source=SOURCE_DATA_API_CURPRICE,
            )

        # ── All tiers exhausted ───────────────────────────────────────────
        self._log.error(
            "canonical_resolver.all_tiers_exhausted",
            asset=asset_u, timeframe=tf, window_ts=window_ts,
            condition_id=(condition_id or "")[:20],
            note="no canonical source available; caller should NOT write outcome",
        )
        return None


# ─── Default on-chain CTF lookup (Web3 RPC) ─────────────────────────────────

# Polygon CTF address — same constant ``scripts/ops/check_pending.py`` uses.
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


def _default_polygon_rpcs() -> list[str]:
    """Return the same fallback RPC list ``check_pending.py`` uses."""
    import os

    rpcs = [
        os.environ.get("POLYGON_RPC_URL"),
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic",
    ]
    return [r for r in rpcs if r]


async def onchain_ctf_payout_lookup(
    condition_id: str,
    *,
    rpcs: Optional[list[str]] = None,
    timeout_secs: float = 10.0,
) -> Optional[str]:
    """Query ``CTF.payoutNumerators(conditionId, index)`` for slots 0/1.

    Polymarket binary markets always have outcome slot 0 = YES (= UP for
    updown markets) and slot 1 = NO (= DOWN). After resolution, exactly one
    slot's numerator is 1, the other 0 (and ``payoutDenominator`` is 1).

    Returns ``"UP"`` / ``"DOWN"`` once a non-zero numerator is found, or
    ``None`` if (a) all RPCs fail, (b) the market hasn't resolved on-chain
    yet (both numerators 0), or (c) ``condition_id`` is malformed.

    Standalone async function — uses ``httpx.AsyncClient`` so it can be
    plugged into ``CanonicalResolver`` via ``onchain_ctf_lookup=...``.
    """
    if not condition_id or not condition_id.startswith("0x") or len(condition_id) != 66:
        return None

    try:
        from eth_abi import encode as abi_encode
        from eth_utils import keccak
    except Exception as exc:  # pragma: no cover — dependency present in prod
        log.warning("canonical_resolver.eth_libs_missing", error=str(exc)[:120])
        return None

    sel = keccak(b"payoutNumerators(bytes32,uint256)")[:4]
    cid_bytes = bytes.fromhex(condition_id[2:])

    url_list = rpcs if rpcs is not None else _default_polygon_rpcs()
    if not url_list:
        return None

    async def _call_slot(slot_idx: int) -> Optional[int]:
        """Try each RPC in order; return first successful int result."""
        import httpx as _httpx

        data = "0x" + (
            sel + abi_encode(["bytes32", "uint256"], [cid_bytes, slot_idx])
        ).hex()
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": _CTF_ADDRESS, "data": data}, "latest"],
        })
        async with _httpx.AsyncClient(timeout=timeout_secs) as client:
            for url in url_list:
                try:
                    resp = await client.post(
                        url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0 NovakashEngine/1.0",
                        },
                    )
                    js = resp.json()
                    if "result" not in js:
                        continue
                    return int(js["result"], 16)
                except Exception:
                    continue
        return None

    # Run both slots concurrently — same RPC client, but two questions.
    try:
        slot_yes, slot_no = await asyncio.gather(
            _call_slot(0), _call_slot(1)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "canonical_resolver.onchain_rpc_failed",
            condition_id=condition_id[:20], error=str(exc)[:120],
        )
        return None

    # Either slot None means RPC failed for that side — refuse to guess.
    if slot_yes is None or slot_no is None:
        return None
    # Both zero means market hasn't resolved on-chain yet.
    if slot_yes == 0 and slot_no == 0:
        return None
    # Standard binary CTF: exactly one slot is 1 post-resolution.
    if slot_yes >= 1 and slot_no == 0:
        return "UP"
    if slot_no >= 1 and slot_yes == 0:
        return "DOWN"
    # Anomaly (both >=1, or some unexpected weighting). Refuse to guess.
    log.warning(
        "canonical_resolver.onchain_anomaly",
        condition_id=condition_id[:20],
        slot_yes=slot_yes, slot_no=slot_no,
    )
    return None
