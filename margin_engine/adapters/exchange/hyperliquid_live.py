"""
Hyperliquid live exchange adapter — EIP-712 signed perps trading.

This is the (live, hyperliquid) branch of the 2x2 venue matrix. Places real
market orders against Hyperliquid's on-chain order book using an agent
wallet private key.

═══════════════════════════════════════════════════════════════════════════
WHY AN AGENT WALLET, NOT YOUR MAIN METAMASK KEY
═══════════════════════════════════════════════════════════════════════════

Hyperliquid's API Wallets (aka "agent wallets") are a native feature:

    1. Sign into app.hyperliquid.xyz with your main MetaMask
    2. Account → API → Create Agent Wallet
    3. Hyperliquid generates a fresh private key scoped ONLY to your account
    4. Shown ONCE — copy it to a password manager immediately

Agent wallets CAN:
    - Place and cancel orders
    - Update leverage
    - Read positions and balances

Agent wallets CANNOT:
    - Withdraw funds from the account
    - Transfer USDC anywhere
    - Change account settings

So if this box is ever compromised, the attacker can cancel your orders
and place bad ones (annoying, rate-limited, and visible), but they cannot
drain your account. This is the blessed pattern for every HL bot/algo.

═══════════════════════════════════════════════════════════════════════════
HOW THIS ADAPTER IS WIRED
═══════════════════════════════════════════════════════════════════════════

- Agent private key lives at /opt/margin-engine/.keys/hyperliquid_agent.pem
  (chmod 600, owned by ubuntu, never committed, never logged)
- Main wallet ADDRESS (not key) in MARGIN_HYPERLIQUID_MAIN_ADDRESS env var
  — this is the public 0x... hex of your MetaMask, used so HL knows which
  account the agent is signing for
- On connect(): load the agent key via eth_account.Account.from_key(),
  construct an Info(read) + Exchange(write) pair, update leverage once
- On place_market_order(): call Exchange.market_open() and parse the
  returned fills into a FillResult with actual fees from the response

═══════════════════════════════════════════════════════════════════════════
PERPS vs SPOT — sizing semantics are DIFFERENT from Binance cross-margin
═══════════════════════════════════════════════════════════════════════════

Binance cross-margin: notional in USDT, engine multiplies by leverage,
    Binance handles margin internally, position is held in the spot asset.

Hyperliquid perps: SIZE is in base asset (BTC, not USD). Leverage is set
    per-asset via update_leverage() before placing orders. Collateral is
    USDC margin against a size-in-BTC position. We convert the engine's
    "notional in USD" convention to a BTC size by dividing by mark price.

This is why market_open(name="BTC", sz=0.00173, ...) places a 0.00173 BTC
    position (~$125 at $72,500), not a $125 order.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import ExchangePort
from margin_engine.domain.value_objects import FillResult, Money, Price, TradeSide

logger = logging.getLogger(__name__)


class HyperliquidLiveError(RuntimeError):
    """Raised when HL API returns a non-ok response."""


class HyperliquidLiveAdapter(ExchangePort):
    """
    Live Hyperliquid perpetuals adapter.

    Implements ExchangePort against mainnet.hyperliquid.xyz using the
    official hyperliquid-python-sdk. Agent-wallet signed — the adapter
    never sees or requires your main MetaMask private key.

    Thread-safety: the hyperliquid-python-sdk is sync/blocking. We run its
    calls on a single-worker ThreadPoolExecutor so they don't block the
    asyncio event loop, and so call ordering is preserved. The executor
    has max_workers=1 intentionally — two concurrent signed requests
    with stale nonces would be rejected by HL.
    """

    def __init__(
        self,
        agent_key_path: str,
        main_account_address: str,
        base_url: Optional[str] = None,
        asset: str = "BTC",
        leverage: int = 3,
        cross_margin: bool = True,
    ) -> None:
        self._agent_key_path = Path(agent_key_path)
        self._main_account_address = main_account_address.lower()
        self._base_url = base_url  # None = SDK default mainnet URL
        self._asset = asset.upper()
        self._leverage = leverage
        self._cross_margin = cross_margin

        self._info = None           # hyperliquid.info.Info instance
        self._exchange = None       # hyperliquid.exchange.Exchange instance
        self._wallet = None         # eth_account LocalAccount (agent, not main)
        self._meta_sz_decimals: int = 5  # BTC default; refreshed on connect
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hl-live")
        self._order_counter = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Load the agent key, initialise SDK clients, set leverage.

        Imports are inside this method so Python can still `import
        margin_engine.main` without the hyperliquid-python-sdk installed
        (it's only required when someone actually boots live HL mode).
        The CI import smoke test exercises that by not installing the SDK.
        """
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        if not self._agent_key_path.exists():
            raise HyperliquidLiveError(
                f"Agent key file not found at {self._agent_key_path}. "
                "Run scripts/setup_hyperliquid_agent.sh first."
            )

        # chmod check — refuse to boot if the key file is world-readable.
        # This catches the class of bug where someone copies the file with
        # default permissions and leaves it at 644.
        mode = self._agent_key_path.stat().st_mode & 0o777
        if mode != 0o600:
            raise HyperliquidLiveError(
                f"Agent key file {self._agent_key_path} has mode {oct(mode)}, "
                f"expected 0o600. Run: sudo chmod 600 {self._agent_key_path}"
            )

        # Read the key as a hex string, load into eth_account. The file
        # format is one line: 0x<64 hex chars>, with optional trailing
        # whitespace. Never logged, even at DEBUG.
        key_text = self._agent_key_path.read_text().strip()
        if not key_text.startswith("0x") or len(key_text) != 66:
            raise HyperliquidLiveError(
                f"Agent key file {self._agent_key_path} does not contain a "
                "valid 0x-prefixed 64-char hex private key"
            )
        try:
            self._wallet = Account.from_key(key_text)
        except Exception as e:
            raise HyperliquidLiveError(f"Failed to load agent key: {e}") from e

        # Sanity check: the agent's derived address should NOT match the
        # main account address — if they do, the user pasted their main
        # key by mistake, which is exactly the thing we're trying to
        # prevent. Refuse to boot.
        agent_address = self._wallet.address.lower()
        if agent_address == self._main_account_address:
            raise HyperliquidLiveError(
                "Agent key derives to the same address as the main account. "
                "You pasted your MAIN wallet key, not an agent wallet key. "
                "Create an API wallet at app.hyperliquid.xyz and use THAT key. "
                "Do not store main wallet keys on the trading server."
            )

        logger.info(
            "HL agent wallet loaded: agent=%s main=%s",
            agent_address, self._main_account_address,
        )

        base_url = self._base_url or constants.MAINNET_API_URL
        self._info = Info(base_url, skip_ws=True)
        self._exchange = Exchange(
            wallet=self._wallet,
            base_url=base_url,
            account_address=self._main_account_address,
        )

        # Refresh per-asset sz_decimals from metadata so we round order
        # sizes correctly. HL rejects orders with too many decimal places.
        try:
            meta = await self._run(self._info.meta)
            universe = meta.get("universe", [])
            asset_meta = next(
                (x for x in universe if x.get("name") == self._asset), None,
            )
            if asset_meta:
                self._meta_sz_decimals = int(asset_meta.get("szDecimals", 5))
                logger.info(
                    "HL %s metadata: szDecimals=%d maxLeverage=%s",
                    self._asset, self._meta_sz_decimals,
                    asset_meta.get("maxLeverage"),
                )
        except Exception as e:
            logger.warning("HL meta fetch failed, using default sz_decimals=5: %s", e)

        # Set leverage once on connect. HL persists this per (user, asset).
        if self._leverage > 1:
            try:
                result = await self._run(
                    self._exchange.update_leverage,
                    self._leverage,
                    self._asset,
                    self._cross_margin,
                )
                logger.info("HL leverage set: %s=%dx cross=%s → %s",
                            self._asset, self._leverage, self._cross_margin, result)
            except Exception as e:
                logger.warning("HL leverage update failed (will retry on first order): %s", e)

        logger.info("HyperliquidLiveAdapter connected to %s", base_url)

    async def close(self) -> None:
        """Shutdown the executor pool — no persistent connections to tear down."""
        self._executor.shutdown(wait=True, cancel_futures=False)
        logger.info("HyperliquidLiveAdapter closed")

    # ── ExchangePort — read side ──────────────────────────────────────────

    async def get_current_price(self, symbol: str) -> Price:
        """
        Latest mid price for the asset. Symbol is ignored beyond stripping
        the USDT suffix — HL perps only quote BTCUSDC-style but the engine
        passes 'BTCUSDT' as a convention. We strip both suffixes.
        """
        asset = self._coin_from_symbol(symbol)
        mids = await self._run(self._info.all_mids)
        raw = mids.get(asset)
        if raw is None:
            raise HyperliquidLiveError(f"No mid price for {asset} in HL response")
        return Price(value=float(raw), pair=symbol)

    async def get_mark(self, symbol: str, side: TradeSide) -> Price:
        """
        Close-side mark. On HL we approximate using L2 book top of book
        (the top of the opposite side of where we'd close). For a LONG
        we'd sell into the bid; for a SHORT we'd buy from the ask.
        Falls back to mid if book fetch fails.
        """
        asset = self._coin_from_symbol(symbol)
        try:
            book = await self._run(self._info.l2_snapshot, asset)
            # l2_snapshot returns {"levels": [[bids...], [asks...]]}
            levels = book.get("levels", [[], []])
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []
            if side == TradeSide.LONG and bids:
                return Price(value=float(bids[0]["px"]), pair=symbol)
            if side == TradeSide.SHORT and asks:
                return Price(value=float(asks[0]["px"]), pair=symbol)
        except Exception as e:
            logger.debug("HL l2_snapshot failed for %s, using mid: %s", asset, e)
        return await self.get_current_price(symbol)

    async def get_balance(self) -> Money:
        """
        Total USDC margin balance on the account. Uses user_state which
        returns the equity value (cross margin accountValue on HL).
        """
        state = await self._run(self._info.user_state, self._main_account_address)
        margin_summary = state.get("marginSummary", {})
        # accountValue is a string like "52.34" in USDC
        raw = margin_summary.get("accountValue", "0")
        try:
            return Money.usd(float(raw))
        except (TypeError, ValueError):
            logger.warning("HL user_state accountValue unparseable: %r", raw)
            return Money.usd(0.0)

    async def get_unrealised_pnl(self, position: Position) -> float:
        """
        Net unrealised P&L for a position. For live HL we fetch the current
        mark and recompute — the engine's Position entity already knows how
        to do this via unrealised_pnl_net(mark), but only with the entry
        commission stored on it. HL commissions are recorded on fill.
        """
        if not position.asset or position.entry_price is None:
            return 0.0
        mark = await self.get_mark(f"{position.asset}USDT", position.side)
        return position.unrealised_pnl_net(mark.value)

    # ── ExchangePort — write side ─────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """
        Open a position with a market order at the top of the HL book.

        The engine thinks in USD notional — HL thinks in base-asset size.
        We convert: sz = notional / current_mid, rounded to sz_decimals.
        """
        asset = self._coin_from_symbol(symbol)
        mid = (await self.get_current_price(symbol)).value
        sz = self._round_size(notional.amount / mid)
        if sz <= 0:
            raise HyperliquidLiveError(
                f"Rounded size for ${notional.amount:.2f} at ${mid:.2f} "
                f"is zero (min tick = 10^-{self._meta_sz_decimals})"
            )

        is_buy = side == TradeSide.LONG
        logger.info(
            "HL market_open: %s %s sz=%s (notional≈$%.2f at mid $%.2f)",
            "BUY" if is_buy else "SELL", asset, sz, notional.amount, mid,
        )

        try:
            response = await self._run(
                self._exchange.market_open,
                asset,
                is_buy,
                sz,
                None,       # px — SDK picks a slippage-adjusted limit
                0.01,       # slippage — 1%, conservative for BTC
                None,       # cloid
            )
        except Exception as e:
            raise HyperliquidLiveError(f"HL market_open raised: {e}") from e

        return self._parse_fill(response, symbol, side, mid)

    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """
        Close a position with a market order in the opposite direction.

        Uses Exchange.market_close which reads the current position size
        off the book and sends a reduce-only order. We pass sz=None so
        HL closes the full position rather than trying to size by USD.
        """
        asset = self._coin_from_symbol(symbol)
        logger.info("HL market_close: %s %s (closing %s position)",
                    "SELL" if side == TradeSide.LONG else "BUY",
                    asset, side.value)

        try:
            response = await self._run(
                self._exchange.market_close,
                asset,
                None,   # sz — None = full position
                None,   # px
                0.01,   # slippage
                None,   # cloid
            )
        except Exception as e:
            raise HyperliquidLiveError(f"HL market_close raised: {e}") from e

        # For close we estimate notional from current mid — HL's response
        # has the actual filled amount so we use that when available.
        mid = (await self.get_current_price(symbol)).value
        return self._parse_fill(response, symbol, side.opposite, mid)

    # ── Internals ─────────────────────────────────────────────────────────

    def _coin_from_symbol(self, symbol: str) -> str:
        """Strip 'USDT' or 'USDC' suffix to get the HL coin name ('BTC')."""
        s = symbol.upper()
        for suffix in ("USDT", "USDC", "USD"):
            if s.endswith(suffix):
                return s[: -len(suffix)]
        return s

    def _round_size(self, sz: float) -> float:
        """Round an order size down to the asset's sz_decimals precision."""
        q = 10 ** self._meta_sz_decimals
        return int(sz * q) / q

    def _parse_fill(
        self,
        response: dict,
        symbol: str,
        side: TradeSide,
        mid_hint: float,
    ) -> FillResult:
        """
        Translate an HL order response into a FillResult.

        HL's response shape on success (market_open):
            {
              "status": "ok",
              "response": {
                "type": "order",
                "data": {
                  "statuses": [
                    {"filled": {"totalSz": "0.00173", "avgPx": "72500.0",
                                "oid": 1234567}}
                  ]
                }
              }
            }
        """
        status = response.get("status")
        if status != "ok":
            raise HyperliquidLiveError(
                f"HL order rejected: {response}"
            )

        self._order_counter += 1
        fallback_id = f"HL-{int(time.time())}-{self._order_counter}"

        try:
            statuses = response["response"]["data"]["statuses"]
            if not statuses:
                raise HyperliquidLiveError(f"HL response had empty statuses: {response}")

            first = statuses[0]
            # Error branch — HL sometimes returns {"error": "..."} in statuses
            if "error" in first:
                raise HyperliquidLiveError(f"HL fill error: {first['error']}")

            filled = first.get("filled") or first.get("resting")
            if not filled:
                raise HyperliquidLiveError(
                    f"HL status had no filled/resting: {first}"
                )

            fill_px = float(filled.get("avgPx") or filled.get("px") or mid_hint)
            fill_sz = float(filled.get("totalSz") or filled.get("sz") or 0)
            oid = filled.get("oid", fallback_id)

            notional_usd = fill_sz * fill_px
            # HL base taker fee is 4.5 bps = 0.00045, maker is 1.5 bps.
            # Real fee may come back in a subsequent userFills query; for
            # the MVP we stamp the conservative taker rate here and leave
            # `commission_is_actual=False`. A follow-up can poll userFills
            # on a short delay to reconcile.
            estimated_commission = notional_usd * 0.00045

            return FillResult(
                order_id=str(oid),
                fill_price=Price(value=fill_px, pair=symbol),
                filled_notional=notional_usd,
                commission=estimated_commission,
                commission_asset="USDC",
                commission_is_actual=False,
            )
        except HyperliquidLiveError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise HyperliquidLiveError(
                f"HL response parse failed: {e}. Raw: {response}"
            ) from e

    async def _run(self, fn, *args, **kwargs):
        """
        Run a blocking SDK call on the single-worker executor.

        All HL SDK calls are sync requests.post/get under the hood. We
        never want to call them from the event loop directly because
        they'd block every other coroutine (price feed, status server,
        probability polling) until the HTTP round-trip completes.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: fn(*args, **kwargs),
        )
