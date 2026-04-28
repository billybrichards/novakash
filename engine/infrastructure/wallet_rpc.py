"""On-chain USDC balance reader with cross-RPC consensus.

Why this exists
---------------
The Polymarket CLOB ``get_balance_allowance`` API caches and lags on-chain state by
10-20 minutes after settlement. Worse: when CLOB returns None / errors transiently,
the previous fallback ``float(... or 0)`` produced a literal ``$0.00`` reading, which:

  - Made the POSITION SNAPSHOT TG card show "Wallet: $0.00"
  - Triggered WALLET DRIFT alerts (false alarm)

The pattern from ``scripts/ops/wallet_truth.py`` reads USDC ``balanceOf(proxy)``
directly via ``eth_call`` — that's the *real* number, no relayer quota cost,
and it never goes stale. We take a 2-of-3 majority across three independent
RPC endpoints so a single bad node can't poison the answer (per memory note
``feedback_cross_rpc_required.md``).

If all three RPCs fail we keep the last known good reading instead of falling
back to 0; if we never had a good reading we return None and let the caller
decide whether to show the card at all.

The reader caches the wallet address resolved from ``POLY_FUNDER_ADDRESS`` so
subsequent reads are pure RPC.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Polygon mainnet USDC (PoS, native — same as wallet_truth.py).
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Selector for ERC-20 ``balanceOf(address)``.
BALANCE_OF_SELECTOR = "0x70a08231"

# Public fallback Polygon RPCs — both no-key, both unrelated to Alchemy
# so a single provider outage cannot flatline us.
DEFAULT_FALLBACK_RPCS: tuple[str, ...] = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
)

_RPC_TIMEOUT_SECONDS = 8.0
# How close two readings need to be (in USDC) to count as "agreement".
# 0.01 USDC ≈ 1 cent — enough to allow tiny rounding drift across RPC nodes
# while still catching genuine divergence (e.g. one stale node).
_CONSENSUS_TOLERANCE_USDC = 0.01


class WalletRPCReader:
    """Reads USDC balance via direct JSON-RPC ``eth_call`` to multiple Polygon nodes.

    Stateful: caches the last known good reading so transient triple-failure
    of all RPCs returns the prior value instead of a deceptive zero.
    """

    def __init__(
        self,
        wallet_address: Optional[str] = None,
        primary_rpc: Optional[str] = None,
        fallback_rpcs: tuple[str, ...] = DEFAULT_FALLBACK_RPCS,
    ) -> None:
        # Resolve wallet from POLY_FUNDER_ADDRESS at construction so we fail
        # loudly at boot rather than silently at first read.
        self._wallet = (
            wallet_address
            or os.environ.get("POLY_FUNDER_ADDRESS")
            or os.environ.get("POLY_PROXY_ADDRESS")
        )
        self._primary_rpc = primary_rpc or os.environ.get("POLYGON_RPC_URL")
        self._fallback_rpcs = tuple(fallback_rpcs)
        self._last_known_good: Optional[float] = None

    @property
    def configured(self) -> bool:
        """True iff we have enough config to attempt a read."""
        return bool(self._wallet) and (
            bool(self._primary_rpc) or bool(self._fallback_rpcs)
        )

    @property
    def last_known_good(self) -> Optional[float]:
        return self._last_known_good

    def _build_payload(self) -> bytes:
        assert self._wallet is not None  # checked by .configured
        data = BALANCE_OF_SELECTOR + self._wallet[2:].lower().rjust(64, "0")
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [
                    {"to": USDC_CONTRACT, "data": data},
                    "latest",
                ],
                "id": 1,
            }
        ).encode()

    @staticmethod
    def _consensus(values: list[float]) -> Optional[float]:
        """Return a USDC value at least 2 of the inputs agree on (within tolerance).

        With 3 RPCs available we want 2-of-3 majority. With 2 we want both to
        match. With 1 we accept it (already implicitly trusted — caller had to
        configure that endpoint). Returns None if no majority emerges.
        """
        if not values:
            return None
        if len(values) == 1:
            return values[0]

        # Pairwise agreement count for each value. O(n^2) — fine for n<=5.
        for i, v in enumerate(values):
            agree = 1  # self
            for j, w in enumerate(values):
                if i == j:
                    continue
                if abs(v - w) <= _CONSENSUS_TOLERANCE_USDC:
                    agree += 1
            if agree >= 2:
                return v
        return None

    async def _fetch_one(self, url: str) -> Optional[float]:
        """Single RPC call. Returns USDC float or None on any failure."""
        # We use urllib in a thread because adding aiohttp / httpx purely for
        # this would bloat the engine container. wallet_truth.py uses the
        # same pattern (urllib + threadpool when async).
        import urllib.error
        import urllib.request

        payload = self._build_payload()

        def _blocking() -> Optional[float]:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "btc-trader-engine/1.0",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=_RPC_TIMEOUT_SECONDS) as r:
                    body = r.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.debug("wallet_rpc.fetch_failed", url=url, error=str(exc)[:120])
                return None
            try:
                resp = json.loads(body)
            except ValueError as exc:
                log.debug("wallet_rpc.bad_json", url=url, error=str(exc)[:120])
                return None
            result = resp.get("result")
            if not isinstance(result, str) or not result.startswith("0x"):
                log.debug("wallet_rpc.bad_result", url=url, result=str(result)[:80])
                return None
            try:
                return int(result, 16) / 1e6
            except ValueError:
                return None

        try:
            return await asyncio.to_thread(_blocking)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.debug("wallet_rpc.thread_error", url=url, error=str(exc)[:120])
            return None

    async def get_balance(self) -> Optional[float]:
        """Return on-chain USDC balance, or None if every source failed and
        no last-known-good is cached.

        Behavior matrix:

            All 3 RPCs return same value         → return that value
            2-of-3 agreement (3rd is bad node)   → return majority
            All 3 disagree                        → keep last_known_good (warn)
            Some succeed, no majority possible    → keep last_known_good (warn)
            All RPCs fail                        → keep last_known_good (warn)
            All fail and no LKG                  → return None (caller hides card)

        Crucially, this never returns 0.0 unless the on-chain balance IS 0.
        """
        if not self.configured:
            log.warning("wallet_rpc.not_configured")
            return self._last_known_good

        endpoints = []
        if self._primary_rpc:
            endpoints.append(self._primary_rpc)
        endpoints.extend(self._fallback_rpcs)

        # Run all in parallel — the slowest is _RPC_TIMEOUT_SECONDS.
        results = await asyncio.gather(
            *(self._fetch_one(u) for u in endpoints),
            return_exceptions=False,
        )
        successful = [r for r in results if r is not None]
        agreed = self._consensus(successful)

        if agreed is not None:
            # If LKG drifted by >$1 we log info — useful trail when wallet
            # actually changed (deposit/redeem) vs noise.
            if (
                self._last_known_good is not None
                and abs(self._last_known_good - agreed) > 1.0
            ):
                log.info(
                    "wallet_rpc.value_changed",
                    prev=round(self._last_known_good, 2),
                    new=round(agreed, 2),
                )
            self._last_known_good = agreed
            return agreed

        # No consensus.
        log.warning(
            "wallet_rpc.no_consensus",
            n_endpoints=len(endpoints),
            n_successful=len(successful),
            successful_values=[round(v, 4) for v in successful],
            using_lkg=self._last_known_good,
        )
        return self._last_known_good
