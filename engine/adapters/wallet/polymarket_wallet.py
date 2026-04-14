"""Wallet balance adapters for live and paper trading modes."""
from __future__ import annotations

import time
import structlog

from domain.ports import WalletBalancePort

log = structlog.get_logger()
_CACHE_TTL = 30.0  # seconds


class PolymarketWalletAdapter(WalletBalancePort):
    """Live USDC balance from Polymarket client, cached 30s."""

    def __init__(self, poly_client, fallback_balance: float = 0.0) -> None:
        self._client = poly_client
        self._fallback = fallback_balance
        self._cached_balance: float | None = None
        self._cache_ts: float = 0.0

    async def get_live_balance(self) -> float:
        now = time.monotonic()
        if self._cached_balance is not None and now - self._cache_ts < _CACHE_TTL:
            return self._cached_balance
        try:
            balance = await self._client.get_balance()
            if balance is not None and balance > 0:
                self._cached_balance = float(balance)
                self._cache_ts = now
                log.info("wallet.balance_fetched", balance=balance)
                return self._cached_balance
        except Exception as exc:
            log.warning("wallet.balance_error", error=str(exc)[:120])
        if self._cached_balance is not None:
            log.warning("wallet.using_stale_cache", balance=self._cached_balance)
            return self._cached_balance
        log.warning("wallet.using_fallback", fallback=self._fallback)
        return self._fallback


class PaperWalletAdapter(WalletBalancePort):
    """Paper mode: reads from risk manager's internal tracking."""

    def __init__(self, risk_manager) -> None:
        self._risk = risk_manager

    async def get_live_balance(self) -> float:
        try:
            status = self._risk.get_status()
            if isinstance(status, dict):
                return float(status.get("current_bankroll", 0.0))
            return float(getattr(status, "current_bankroll", 0.0))
        except Exception as exc:
            log.warning("paper_wallet.error", error=str(exc)[:80])
            return 0.0
