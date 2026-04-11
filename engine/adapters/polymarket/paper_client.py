"""
Paper Polymarket client -- simulates CLOB orders without real money.

Extracted from ``engine/execution/polymarket_client.PolymarketClient``
(the ``paper_mode=True`` branches). Follows the margin_engine pattern:
``margin_engine/adapters/exchange/paper.py`` (PaperExchangeAdapter).

All behavior is copied VERBATIM from the monolith -- zero logic changes.
The composition root selects this adapter when paper_mode is True.

Implements ``PolymarketClientPort`` from ``engine/domain/ports.py``.
Also exposes every public method from the original PolymarketClient so
callers that depend on the concrete class (strategies, orchestrator,
SOT reconciler) keep working once wired through the composition root.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import structlog

from engine.domain.ports import PolymarketClientPort

# Re-use the PolyOrderStatus data class from the original module so both
# adapters share a single canonical type.  When the domain model is
# fleshed out this will move into engine/domain/value_objects.py.
from engine.execution.polymarket_client import PolyOrderStatus

logger = structlog.get_logger(__name__)

# 5-minute prediction windows
POLY_WINDOW_SECONDS = 300

# Simulated paper balance (USD)
_PAPER_BALANCE_USD = 10_000.0


class PaperPolymarketClient(PolymarketClientPort):
    """
    Paper-mode Polymarket client -- simulates all CLOB interactions locally.

    Tracks a virtual balance, simulates fills with random slippage,
    and maintains an in-memory order book. No network calls, no API keys.

    Follows the PaperExchangeAdapter pattern from margin_engine:
    same interface shape, simulated internals, deterministic enough
    for strategy back-testing while realistic enough to catch
    order-flow bugs.
    """

    def __init__(self) -> None:
        # Paper-mode state
        self._paper_balance: float = _PAPER_BALANCE_USD
        self._paper_orders: dict[str, dict] = {}

        self._log = logger.bind(component="polymarket_client", paper_mode=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Paper mode just logs startup -- no real connection needed."""
        self._log.info("polymarket_client.connected", mode="paper")

    # ------------------------------------------------------------------
    # High-level convenience -- called by strategies
    # ------------------------------------------------------------------

    async def place_order(
        self,
        market_slug: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
        token_id: Optional[str] = None,
    ) -> str:
        """Simulate a directional order on a binary market.

        Args:
            market_slug: Human-readable market identifier.
            direction: "YES" or "NO".
            price: Expected fill price in [0, 1].
            stake_usd: Notional USD amount to risk.
            token_id: Ignored in paper mode.

        Returns:
            Order ID string ("paper-<uuid>").
        """
        direction = direction.upper()
        if direction not in {"YES", "NO"}:
            raise ValueError(f"direction must be YES or NO, got {direction!r}")

        self._log.info(
            "place_order.requested",
            market_slug=market_slug,
            direction=direction,
            price=str(price),
            stake_usd=stake_usd,
        )

        # +/-0.5% random slippage
        slippage = Decimal(str(random.uniform(-0.005, 0.005)))
        fill_price = max(Decimal("0.01"), min(Decimal("0.99"), price + slippage))

        shares = float(stake_usd) / float(fill_price)
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        ts = time.time()

        self._paper_orders[order_id] = {
            "order_id": order_id,
            "market_slug": market_slug,
            "direction": direction,
            "requested_price": str(price),
            "fill_price": str(fill_price),
            "stake_usd": stake_usd,
            "shares": shares,
            "status": "FILLED",
            "created_at": ts,
            "filled_at": ts,
        }
        self._paper_balance -= stake_usd

        self._log.info(
            "place_order.paper_filled",
            order_id=order_id,
            fill_price=str(fill_price),
            slippage_pct=f"{float(slippage)*100:.3f}%",
            shares=f"{shares:.4f}",
        )
        return order_id

    # ------------------------------------------------------------------
    # FOK Ladder helpers -- v8.0 Phase 2
    # ------------------------------------------------------------------

    async def get_clob_best_ask(self, token_id: str) -> float:
        """Return a simulated best-ask price around 0.50.

        Paper mode has unlimited simulated liquidity -- no real book.
        """
        simulated = round(random.uniform(0.40, 0.65), 4)
        self._log.debug(
            "get_clob_best_ask.paper",
            token_id=token_id[:20] + "...",
            simulated_ask=f"${simulated:.4f}",
        )
        return simulated

    async def place_fok_order(
        self,
        token_id: str,
        price: float,
        size: float,
    ) -> dict:
        """Simulate a Fill-or-Kill (FOK) order -- always fills in paper mode.

        Paper mode has unlimited simulated liquidity, so every FOK fills.
        Realistic FOK testing requires live mode.

        Returns:
            Dict: {filled: bool, size_matched: float, order_id: str}
        """
        simulated_order_id = f"fok-paper-{uuid.uuid4().hex[:12]}"
        self._log.info(
            "place_fok_order.paper",
            token_id=token_id[:20] + "...",
            price=f"${price:.4f}",
            size=f"{size:.2f}",
            order_id=simulated_order_id,
        )
        return {
            "filled": True,
            "size_matched": size,
            "order_id": simulated_order_id,
        }

    async def place_market_order(
        self,
        token_id: str,
        price: float,
        size: float,
        order_type: str = "FAK",
    ) -> dict:
        """Simulate a market order (FAK/FOK) -- always fills in paper mode.

        Returns:
            dict with keys: filled (bool), size_matched (float), order_id (str).
        """
        _sim_size = math.floor(size * 100) / 100
        return {
            "filled": True,
            "size_matched": _sim_size,
            "order_id": f"paper-{order_type.lower()}-{uuid.uuid4().hex[:8]}",
        }

    async def get_order_book_spread(self, token_id: str) -> float:
        """Return default 2c spread assumption for paper mode."""
        return 0.02

    def calculate_dynamic_bump(self, base_price: float, spread: float) -> float:
        """Calculate dynamic price bump based on order book spread.

        Strategy: bump by min(2c, spread) -- do not overpay more than the spread,
        but always bump at least 0.5c to cross the spread.
        """
        bump = min(0.02, max(0.005, spread))
        bumped = round(base_price + bump, 4)
        self._log.info(
            "price.dynamic_bump",
            base=f"${base_price:.4f}",
            spread=f"${spread:.4f}",
            bump=f"${bump:.4f}",
            bumped=f"${bumped:.4f}",
        )
        return bumped

    async def place_rfq_order(
        self,
        token_id: str,
        direction: str,
        price: float,
        size: float,
        max_price: float = 0.65,
    ) -> tuple[str | None, float | None]:
        """Simulate an RFQ order -- returns instant fill at requested price."""
        self._log.info("rfq.paper_mode", price=f"${price:.4f}", size=f"{size:.1f}")
        return (f"rfq-paper-{uuid.uuid4().hex[:12]}", price)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_current_market_slug(self) -> str:
        """Return the slug for the current 5-minute BTC up/down window."""
        now = int(time.time())
        window_ts = now - (now % POLY_WINDOW_SECONDS)
        return f"btc-updown-5m-{window_ts}"

    async def get_market_prices(self, market_slug: str) -> dict[str, Decimal]:
        """Simulate a slightly noisy 50/50 market."""
        yes_price = Decimal(str(round(random.uniform(0.46, 0.54), 4)))
        no_price = Decimal("1.0000") - yes_price
        self._log.debug(
            "get_market_prices.paper",
            market_slug=market_slug,
            yes=str(yes_price),
            no=str(no_price),
        )
        return {"yes": yes_price, "no": no_price}

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Return simulated paper balance."""
        self._log.debug("get_balance.paper", balance=self._paper_balance)
        return self._paper_balance

    async def get_order_status(self, order_id: str) -> dict:
        """Return status dict for a paper order."""
        if order_id not in self._paper_orders:
            return {"order_id": order_id, "status": "NOT_FOUND"}
        return dict(self._paper_orders[order_id])

    async def get_portfolio_value(self) -> float:
        """Paper mode portfolio value is just the simulated cash balance."""
        return await self.get_balance()

    async def get_position_outcomes(self) -> dict:
        """Paper mode has no real positions to fetch."""
        return {}

    async def get_open_orders(self) -> list[dict]:
        """Return all tracked paper orders."""
        return list(self._paper_orders.values())

    async def get_trade_history(self) -> list[dict]:
        """Paper mode has no real trade fills to fetch."""
        return []

    # ------------------------------------------------------------------
    # POLY-SOT methods
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_status(raw_status: Any) -> str:
        """Map Polymarket status string to the SOT alphabet."""
        if raw_status is None:
            return "unknown"
        s = str(raw_status).strip().lower()
        if not s or s == "unknown":
            return "unknown"
        if s == "matched":
            return "matched"
        if s == "filled":
            return "filled"
        if s in ("live", "delayed", "pending", "open"):
            return "pending"
        if s in ("cancelled", "canceled", "expired"):
            return "cancelled"
        if s in ("rejected", "unmatched", "failed"):
            return "rejected"
        return "unknown"

    @classmethod
    def _parse_order_status(cls, order_id: str, resp: Any) -> PolyOrderStatus:
        """Coerce a raw CLOB get_order response into a typed PolyOrderStatus."""
        def _get(obj, key, default=None):
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        status = cls._normalise_status(_get(resp, "status", "unknown"))

        fill_size: Optional[float] = None
        for key in ("size_matched", "sizeMatched", "takingAmount", "filled_size"):
            val = _get(resp, key)
            if val is not None and val != "":
                try:
                    fill_size = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        fill_price: Optional[float] = None
        for key in ("avgFillPrice", "average_price", "fill_price", "price"):
            val = _get(resp, key)
            if val is not None and val != "":
                try:
                    fill_price = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        ts: Optional[datetime] = None
        for key in ("match_time", "matchTime", "updated_at", "updatedAt", "created_at", "createdAt"):
            val = _get(resp, key)
            if val is None:
                continue
            try:
                if isinstance(val, (int, float)):
                    ts = datetime.fromtimestamp(float(val), tz=timezone.utc)
                else:
                    s = str(val).replace("Z", "+00:00")
                    ts = datetime.fromisoformat(s)
                break
            except (ValueError, TypeError, OSError):
                continue

        return PolyOrderStatus(
            order_id=order_id,
            status=status,
            fill_price=fill_price,
            fill_size=fill_size,
            timestamp=ts,
            raw=resp if isinstance(resp, dict) else None,
        )

    async def get_order_status_sot(self, order_id: str) -> Optional[PolyOrderStatus]:
        """SOT-grade order status fetch for paper mode."""
        if not order_id:
            return None

        paper = self._paper_orders.get(order_id)
        if paper is None:
            if order_id.startswith("manual-paper-"):
                return PolyOrderStatus(
                    order_id=order_id,
                    status="filled",
                    fill_price=None,
                    fill_size=None,
                    timestamp=datetime.now(timezone.utc),
                    raw={"synthetic_paper": True},
                )
            return None
        try:
            fill_price = float(paper.get("fill_price")) if paper.get("fill_price") is not None else None
        except (ValueError, TypeError):
            fill_price = None
        try:
            fill_size = float(paper.get("shares")) if paper.get("shares") is not None else None
        except (ValueError, TypeError):
            fill_size = None
        ts: Optional[datetime] = None
        try:
            if paper.get("filled_at"):
                ts = datetime.fromtimestamp(float(paper["filled_at"]), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            ts = None
        return PolyOrderStatus(
            order_id=order_id,
            status="filled" if str(paper.get("status", "")).upper() == "FILLED" else "pending",
            fill_price=fill_price,
            fill_size=fill_size,
            timestamp=ts,
            raw=paper,
        )

    async def list_recent_orders(
        self, since: Optional[datetime] = None, limit: int = 50,
    ) -> list[PolyOrderStatus]:
        """Bulk SOT helper -- return parsed PolyOrderStatus for paper orders."""
        results: list[PolyOrderStatus] = []
        for oid, p in self._paper_orders.items():
            ts: Optional[datetime] = None
            try:
                if p.get("created_at"):
                    ts = datetime.fromtimestamp(float(p["created_at"]), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                ts = None
            if since is not None and ts is not None and ts < since:
                continue
            try:
                fp = float(p.get("fill_price")) if p.get("fill_price") is not None else None
            except (ValueError, TypeError):
                fp = None
            try:
                fs = float(p.get("shares")) if p.get("shares") is not None else None
            except (ValueError, TypeError):
                fs = None
            results.append(
                PolyOrderStatus(
                    order_id=oid,
                    status="filled" if str(p.get("status", "")).upper() == "FILLED" else "pending",
                    fill_price=fp,
                    fill_size=fs,
                    timestamp=ts,
                    raw=p,
                )
            )
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # PolymarketClientPort abstract methods (domain port compliance)
    # ------------------------------------------------------------------

    async def get_window_market(self, asset: str, window_ts: int):
        """Paper stub -- returns None (no Gamma market lookup in paper mode)."""
        return None

    async def get_book(self, token_id: str):
        """Paper stub -- returns None (no live CLOB book in paper mode)."""
        return None

    async def poll_pending_trades(self) -> list:
        """Paper stub -- returns empty list."""
        return []
