"""
On-Chain Position Redeemer — Builder Relayer Edition

Redeems resolved Polymarket positions back to USDC by calling
redeemPositions() on the Conditional Tokens Framework (CTF) contract,
executed via the Builder Relayer API (supports Magic.link proxy wallets).

Flow:
1. Fetch positions from Polymarket data API
2. Filter for resolved positions (curPrice <= 0.01 or >= 0.99)
3. Build redeemPositions calldata for each resolved position
4. Submit via Builder Relayer SDK (RelayClient.execute)
5. Poll for CONFIRMED status, report USDC balance change

Replaces the Gnosis Safe execTransaction approach which does NOT work
with Magic.link proxy wallets.

Contracts:
  - CTF:  0x4D97DCd97eC945f40cF65F87097ACe5EA0476045 (Polygon)
  - USDC: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 (Polygon PoS)
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import structlog

log = structlog.get_logger(__name__)


# ── Rate-limit handling ───────────────────────────────────────────────────────
#
# The Polygon Builder Relayer API returns HTTP 429 when the wallet's daily
# quota is exhausted, with an error body shaped like:
#
#     RelayerApiException[status_code=429, error_message={
#         'error': 'quota exceeded: 0 units remaining, resets in 9906 seconds'
#     }]
#
# Before this module had cooldown awareness, the redeemer loop kept calling
# once per settled position every 5 minutes, burning through dozens of
# no-op 429 calls per hour and potentially keeping the relayer quota
# perpetually at 0 (each call appears to cost 1 unit even on failure, from
# what we observed on 2026-04-10).
#
# The fix: parse "resets in N seconds" from the error string, set an
# instance-level cooldown expiry, and short-circuit all redemption calls
# until the cooldown expires. Falls back to a 5-minute cooldown if the
# parse fails, so we never end up in a tight retry loop.
_RATE_LIMIT_MARKERS = ("429", "quota exceeded", "units remaining")
_RESET_REGEX = re.compile(r"resets?\s+in\s+(\d+)\s*seconds?", re.IGNORECASE)
_DEFAULT_COOLDOWN_SECONDS = 300  # 5 minutes when we can't parse the header


def _is_rate_limit_error(exc_str: str) -> bool:
    """True if the error string looks like a Polygon Relayer rate-limit response.

    Substring match against multiple markers so we catch the 429 regardless
    of whether the SDK surfaces `status_code=429`, the literal quota text,
    or the `units remaining` phrasing. All three appear in the real error
    strings we've seen from py_builder_relayer_client.
    """
    if not exc_str:
        return False
    lower = exc_str.lower()
    return any(marker in lower for marker in _RATE_LIMIT_MARKERS)


def _parse_reset_seconds(exc_str: str) -> Optional[int]:
    """
    Extract the 'resets in N seconds' value from a Builder Relayer 429 error.

    Real-world inputs look like:
        "RelayerApiException[status_code=429, error_message={'error': \
         'quota exceeded: 0 units remaining, resets in 9906 seconds'}]"

    Returns:
        The parsed N as an int if found and sane (0 < N < 86400 seconds = 24h).
        None if no parse was possible — callers should then apply their own
        default cooldown rather than retry immediately.

    Design notes:
        - Uses a loose regex (`resets? in N seconds?`) so slight phrasing
          drift in the upstream API doesn't break the parse.
        - Caps at 24h as a sanity guard: if the Relayer ever returns a
          wildly large value (e.g. an epoch-seconds bug upstream), we
          clamp to the default cooldown instead of sitting idle for days.
        - Returns None on zero or negative values — we'd rather fall back
          to the default than interpret "0 seconds" as "retry immediately".
    """
    if not exc_str:
        return None
    match = _RESET_REGEX.search(exc_str)
    if not match:
        return None
    try:
        n = int(match.group(1))
    except (TypeError, ValueError):
        return None
    if n <= 0 or n > 86400:
        return None
    return n

# ── Contract Addresses (Polygon Mainnet) ──────────────────────────────────────
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ── Minimal ABIs ──────────────────────────────────────────────────────────────
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

ERC20_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class PositionRedeemer:
    """
    Redeems resolved Polymarket positions via the Builder Relayer API.

    Works with Magic.link proxy wallets (unlike direct Gnosis Safe calls).

    Requires:
    - Polygon RPC URL (for web3 contract calls / USDC balance)
    - Private key of the EOA that owns the proxy wallet
    - Proxy (funder) wallet address on Polymarket
    - Builder Relayer API key (BUILDER_KEY env var or builder_key param)

    All operations are async. Synchronous SDK calls are wrapped in asyncio.to_thread().
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        proxy_address: str,
        paper_mode: bool = True,
        builder_key: Optional[str] = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._paper_mode = paper_mode
        self._builder_key = builder_key or os.environ.get("BUILDER_API_KEY", "") or os.environ.get("BUILDER_KEY", "")
        self._builder_secret = os.environ.get("BUILDER_SECRET", "")
        self._builder_passphrase = os.environ.get("BUILDER_PASSPHRASE", "")
        self._w3 = None
        self._ctf = None
        self._usdc = None
        self._relay_client = None
        self._log = log.bind(component="redeemer", paper_mode=paper_mode)

        # Rate-limit cooldown state (see _parse_reset_seconds docstring).
        # `_rate_limit_until` is an absolute UTC deadline — while now() <
        # this value, redemption calls short-circuit and return early.
        # Reset to None after a successful call or when the deadline passes.
        self._rate_limit_until: Optional[datetime] = None
        self._rate_limit_reason: str = ""
        # Throttle for "cooldown active" log lines so the engine log
        # doesn't fill up during a multi-hour 429 window.
        self._last_cooldown_log_at: Optional[datetime] = None

    # ── Cooldown helpers ──────────────────────────────────────────────────────

    def _in_cooldown(self) -> bool:
        """True iff we're currently in a rate-limit cooldown window."""
        if self._rate_limit_until is None:
            return False
        now = datetime.now(timezone.utc)
        if now >= self._rate_limit_until:
            # Cooldown has expired naturally — clear state and return False.
            self._log.info(
                "redeemer.cooldown_cleared",
                expired_at=self._rate_limit_until.isoformat(),
                reason=self._rate_limit_reason[:80],
            )
            self._rate_limit_until = None
            self._rate_limit_reason = ""
            self._last_cooldown_log_at = None
            return False
        return True

    def _log_cooldown_active(self) -> None:
        """Log a throttled 'cooldown active' message (at most once per minute)."""
        if self._rate_limit_until is None:
            return
        now = datetime.now(timezone.utc)
        if self._last_cooldown_log_at is not None:
            if (now - self._last_cooldown_log_at).total_seconds() < 60:
                return
        self._last_cooldown_log_at = now
        remaining = int((self._rate_limit_until - now).total_seconds())
        self._log.info(
            "redeemer.cooldown_active",
            remaining_seconds=max(0, remaining),
            resets_at=self._rate_limit_until.isoformat(),
            reason=self._rate_limit_reason[:120],
        )

    def _trip_cooldown(self, exc_str: str) -> None:
        """Enter cooldown after detecting a rate-limit error.

        Parses the 'resets in N seconds' value from the error string via
        `_parse_reset_seconds`. If no parse is possible, falls back to
        `_DEFAULT_COOLDOWN_SECONDS` (5 minutes). Always logs the transition
        so an operator can see exactly how long the cooldown will last.
        """
        reset_seconds = _parse_reset_seconds(exc_str) or _DEFAULT_COOLDOWN_SECONDS
        now = datetime.now(timezone.utc)
        self._rate_limit_until = now + timedelta(seconds=reset_seconds)
        self._rate_limit_reason = exc_str[:200]
        self._last_cooldown_log_at = None  # allow one immediate log after trip
        self._log.warning(
            "redeemer.cooldown_tripped",
            cooldown_seconds=reset_seconds,
            resets_at=self._rate_limit_until.isoformat(),
            parsed_from_error=bool(_parse_reset_seconds(exc_str)),
            reason_preview=exc_str[:120],
        )

    async def connect(self) -> None:
        """Initialise web3 and Builder Relayer client."""
        if self._paper_mode:
            self._log.info("redeemer.skip_connect", reason="paper_mode")
            return

        if not self._rpc_url:
            self._log.warning("redeemer.no_rpc_url")
            return

        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))

            self._ctf = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_ABI,
            )
            self._usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=ERC20_BALANCE_ABI,
            )

            chain_id = self._w3.eth.chain_id
            self._log.info(
                "redeemer.web3_connected",
                chain_id=chain_id,
                proxy=self._proxy_address,
            )
        except Exception as exc:
            self._log.error("redeemer.web3_connect_failed", error=str(exc))

        # Initialise Builder Relayer client
        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig

            if not self._builder_key:
                self._log.warning("redeemer.no_builder_key", hint="Set BUILDER_KEY env var")
                return

            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            creds = BuilderApiKeyCreds(
                key=self._builder_key,
                secret=self._builder_secret,
                passphrase=self._builder_passphrase,
            )
            config = BuilderConfig(
                local_builder_creds=creds,
            )
            from py_builder_relayer_client.models import RelayerTxType
            self._relay_client = RelayClient(
                relayer_url="https://relayer-v2.polymarket.com",
                chain_id=137,
                private_key=self._private_key,
                builder_config=config,
                relay_tx_type=RelayerTxType.PROXY,
            )
            self._log.info("redeemer.relay_client_ready")
        except Exception as exc:
            self._log.error("redeemer.relay_client_failed", error=str(exc))

    async def get_usdc_balance(self) -> float:
        """Get USDC balance of the proxy wallet (6 decimals)."""
        if not self._w3 or not self._usdc:
            return 0.0
        try:
            from web3 import Web3

            balance_raw = await asyncio.to_thread(
                self._usdc.functions.balanceOf(
                    Web3.to_checksum_address(self._proxy_address)
                ).call
            )
            return balance_raw / 1e6
        except Exception as exc:
            self._log.debug("redeemer.usdc_balance_error", error=str(exc))
            return 0.0

    async def fetch_redeemable_positions(self) -> list[dict]:
        """
        Fetch positions from Polymarket data API and filter for redeemable ones.

        A position is redeemable when curPrice <= 0.01 (loss) or >= 0.99 (win).
        Both wins and losses are redeemed — losses clear the accounting books.

        Returns list of dicts with: conditionId, tokenId, size, outcome, pnl, curPrice
        Returns empty list if we're currently in a rate-limit cooldown — the
        position scan itself doesn't hit the relayer, but there's no point
        staging a sweep we can't execute.
        """
        if self._paper_mode:
            return []
        if self._in_cooldown():
            self._log_cooldown_active()
            return []

        try:
            funder = self._proxy_address.lower()
            url = f"https://data-api.polymarket.com/positions?user={funder}"
            headers = {"User-Agent": "NovakashEngine/1.0"}

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        self._log.warning("redeemer.positions_api_error", status=resp.status)
                        return []
                    positions = await resp.json()

            if not positions:
                return []

            redeemable = []

            for p in positions:
                cur_price = float(p.get("curPrice", 0.5))
                size = float(p.get("size", 0))
                condition_id = p.get("conditionId", "")

                # Skip open positions (price still between 0.01 and 0.99)
                if 0.01 < cur_price < 0.99:
                    continue

                # Skip zero-size positions
                if size <= 0:
                    continue

                if not condition_id:
                    continue

                outcome = "WIN" if cur_price >= 0.99 else "LOSS"
                avg_price = float(p.get("avgPrice", 0))
                pnl = (size * cur_price) - (size * avg_price)

                redeemable.append({
                    "conditionId": condition_id,
                    "size": size,
                    "avgPrice": avg_price,
                    "curPrice": cur_price,
                    "outcome": outcome,
                    "pnl": pnl,
                    "tokenId": p.get("tokenId", ""),
                    "asset": p.get("asset", ""),
                })

            self._log.info(
                "redeemer.scan_complete",
                total_positions=len(positions),
                redeemable=len(redeemable),
                wins=sum(1 for p in redeemable if p["outcome"] == "WIN"),
                losses=sum(1 for p in redeemable if p["outcome"] == "LOSS"),
            )
            return redeemable

        except Exception as exc:
            self._log.error("redeemer.scan_error", error=str(exc))
            return []

    async def redeem_position(self, condition_id: str) -> bool:
        """
        Redeem a single resolved position via Builder Relayer.

        Steps:
        1. Build redeemPositions calldata (collateral=USDC, indexSets=[1,2])
        2. Wrap in SafeTransaction
        3. Submit via RelayClient.execute (synchronous → asyncio.to_thread)
        4. Poll until CONFIRMED or FAILED

        Returns True if redemption confirmed. If in a rate-limit cooldown,
        returns False immediately without calling the relayer.
        """
        if self._paper_mode or not self._relay_client or not self._ctf:
            return False

        # ── Cooldown gate: skip if a previous 429 is still in effect. ─────
        # Returns False so redeem_all() counts this as "failed" — but
        # without consuming a relayer quota unit or spamming the log.
        if self._in_cooldown():
            self._log_cooldown_active()
            return False

        try:
            from web3 import Web3
            from py_builder_relayer_client.models import Transaction

            cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            zero_bytes32 = b"\x00" * 32

            # Build redeemPositions calldata
            fn = self._ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                zero_bytes32,   # parentCollectionId (0 for root collection)
                cid_bytes,      # conditionId
                [1, 2],         # indexSets: YES=1, NO=2
            )
            calldata = fn._encode_transaction_data()

            # Use Transaction (SDK handles Safe vs Proxy based on relay_tx_type)
            txn = Transaction(
                to=CTF_ADDRESS,
                data=calldata,
                value="0",
            )

            # Execute via relay (synchronous SDK call — handles signing internally)
            response = await asyncio.to_thread(
                self._relay_client.execute, [txn]
            )

            tx_id = getattr(response, "transactionId", None)
            tx_hash = getattr(response, "transactionHash", None)

            self._log.info(
                "redeemer.submitted",
                condition=condition_id[:20] + "...",
                tx_id=tx_id,
                tx_hash=tx_hash,
            )

            if not tx_id:
                self._log.warning("redeemer.no_tx_id", condition=condition_id[:20])
                return False

            # Poll for confirmation (synchronous poll wrapped in thread)
            confirmed = await asyncio.to_thread(
                self._relay_client.poll_until_state,
                tx_id,
                ["CONFIRMED"],
                "FAILED",
                20,     # max_polls
                3000,   # poll_frequency ms
            )

            success = bool(confirmed)

            self._log.info(
                "redeemer.redeem_result",
                condition=condition_id[:20] + "...",
                success=success,
                tx_hash=tx_hash,
            )

            return success

        except Exception as exc:
            exc_str = str(exc)
            if _is_rate_limit_error(exc_str):
                # Trip global cooldown — every other pending redemption
                # in this sweep will now short-circuit at `_in_cooldown()`.
                self._trip_cooldown(exc_str)
            else:
                # Real error, not a rate limit. Log at error level so it
                # triggers alerts; the cooldown path logs at warning.
                self._log.error(
                    "redeemer.redeem_error",
                    condition=condition_id[:20] + "...",
                    error=exc_str[:200],
                )
            return False

    async def redeem_all(self) -> dict:
        """
        Scan for redeemable positions and redeem them all (wins AND losses).

        Losses are redeemed to clear accounting — they return 0 USDC but
        must be settled on-chain.

        Returns summary dict:
        {
            "scanned": int,
            "redeemed": int,
            "failed": int,
            "wins": int,
            "losses": int,
            "total_pnl": float,
            "usdc_before": float,
            "usdc_after": float,
            "tx_hashes": list[str],
            "paper_mode": bool,
        }
        """
        if self._paper_mode:
            return {
                "scanned": 0,
                "redeemed": 0,
                "failed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "usdc_before": 0.0,
                "usdc_after": 0.0,
                "tx_hashes": [],
                "paper_mode": True,
            }

        # Pre-flight cooldown check: if we already know we're rate-limited,
        # skip the scan entirely so we don't even burn a positions-API call.
        if self._in_cooldown():
            self._log_cooldown_active()
            return {
                "scanned": 0,
                "redeemed": 0,
                "failed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "usdc_before": 0.0,
                "usdc_after": 0.0,
                "tx_hashes": [],
                "paper_mode": False,
                "cooldown_active": True,
                "cooldown_remaining_seconds": int(
                    (self._rate_limit_until - datetime.now(timezone.utc)).total_seconds()
                ) if self._rate_limit_until else 0,
            }

        usdc_before = await self.get_usdc_balance()

        positions = await self.fetch_redeemable_positions()
        if not positions:
            return {
                "scanned": 0,
                "redeemed": 0,
                "failed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "usdc_before": usdc_before,
                "usdc_after": usdc_before,
                "tx_hashes": [],
                "paper_mode": False,
            }

        redeemed = 0
        failed = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        tx_hashes: list[str] = []

        for pos in positions:
            try:
                success = await self.redeem_position(pos["conditionId"])
                if success:
                    redeemed += 1
                    total_pnl += pos["pnl"]
                    if pos["outcome"] == "WIN":
                        wins += 1
                    else:
                        losses += 1
                else:
                    failed += 1
            except Exception as exc:
                self._log.error(
                    "redeemer.sweep_position_error",
                    condition=pos.get("conditionId", "?")[:20],
                    error=str(exc),
                )
                failed += 1

            # Small delay between redemptions to avoid relay rate limits
            await asyncio.sleep(2)

        usdc_after = await self.get_usdc_balance()

        self._log.info(
            "redeemer.sweep_complete",
            redeemed=redeemed,
            wins=wins,
            losses=losses,
            failed=failed,
            total_pnl=f"${total_pnl:.2f}",
            usdc_change=f"${usdc_after - usdc_before:.2f}",
        )

        return {
            "scanned": len(positions),
            "redeemed": redeemed,
            "failed": failed,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "usdc_before": usdc_before,
            "usdc_after": usdc_after,
            "tx_hashes": tx_hashes,
            "paper_mode": False,
        }
