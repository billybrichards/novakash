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

# ── Exponential backoff on top of the server-reported cooldown ────────────────
#
# Even after tripping the 24 h quota cooldown, each additional 429 inside the
# same cooldown window burns one unit (Polymarket Relayer observed behaviour —
# 2026-04-10). To prevent the engine from "hot-looping" on a 429 and eroding
# tomorrow's quota, we layer a per-session exponential backoff ON TOP OF
# ``_rate_limit_until`` (the server-reported cooldown).
#
# Formula: ``min(BASE * 2^N, CAP)`` where N = consecutive 429 count.
# N resets to 0 on any non-429 success.
#
# BASE=30s, CAP=30min. With BASE=30s the first 429 yields 30s, the second 60s,
# the third 2m, etc., capping at 30m after ~6 consecutive 429s. The CAP is
# significantly shorter than a typical ``resets in N seconds`` countdown
# (often several hours), so the outer cooldown dominates — the backoff only
# kicks in if the redeemer tripped a 429 without a parseable reset header.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 30 * 60  # 30 min


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
        attempts_repo: Optional[object] = None,
        trades_repo: Optional[object] = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._paper_mode = paper_mode
        self._builder_key = (
            builder_key
            or os.environ.get("BUILDER_API_KEY", "")
            or os.environ.get("BUILDER_KEY", "")
        )
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
        # Conservative hard cap — Daisy-set 2026-04-16. Server-side allowance
        # is 100/rolling-24h but we want headroom for manual sweeps + safety
        # against burn cascades during NegRisk-slow afternoons. Override via
        # REDEEM_DAILY_LIMIT env if needed, but keep the default at 80.
        self._daily_quota_limit: int = int(
            os.environ.get("REDEEM_DAILY_LIMIT", "80") or 80
        )
        # Per-hour throttle (Daisy-set 2026-04-16). Default 4 wins/hour keeps
        # the automatic sweep well below the relayer's observed rate-limit
        # window. Manual redeem_wins / redeem_all still bypasses this cap.
        self._hourly_quota_limit: int = int(
            os.environ.get("REDEEM_HOURLY_LIMIT", "4") or 4
        )
        self._last_loss_sweep_at: Optional[datetime] = None

        # Exponential-backoff state layered ON TOP OF _rate_limit_until.
        # ``_consecutive_429`` is the count of consecutive 429s in this
        # session — resets to 0 on any non-429 success. ``_backoff_until``
        # is an absolute UTC deadline; while now() < this value the
        # redeemer short-circuits (alongside the server-reported cooldown).
        self._consecutive_429: int = 0
        self._backoff_until: Optional[datetime] = None

        # Optional attempts repo — when wired, redeem_position() skips any
        # condition_id with >= REDEEM_MAX_FAILURES_24H failed attempts in
        # the last 24 h, preventing hot-loops on stuck positions.
        self._attempts_repo = attempts_repo
        self._max_failures_24h: int = int(
            os.environ.get("REDEEM_MAX_FAILURES_24H", "3") or 3
        )

        # Optional trades repo — when wired, successful redemptions flip
        # ``trades.redeemed``/``redemption_tx``/``redeemed_at`` so audits,
        # dashboards, and future sweeps can distinguish truly-settled
        # positions from stale "WIN" rows that never got swept. Without
        # this repo the sweep still works on-chain but the trades table
        # is a poor source of truth (see the April 18 2026 postmortem:
        # 1,285 cumulative WIN rows, zero with redeemed=true or a
        # redemption_tx populated).
        self._trades_repo = trades_repo

    @property
    def daily_quota_limit(self) -> int:
        return self._daily_quota_limit

    @property
    def hourly_quota_limit(self) -> int:
        return self._hourly_quota_limit

    async def _scan_redeemable_positions(self) -> list[dict]:
        """
        Read-only scan of currently redeemable WIN positions.

        Returns a list of ``{condition_id, value, resolved_at}`` dicts —
        the projection consumed by ``pending_wins_summary()``.

        Implementation notes:
          - Delegates to ``fetch_redeemable_positions(outcomes={"WIN"})``
            so both the actual sweep (``redeem_wins``) and this read-only
            view see the SAME set of positions. No drift between what we
            REPORT as pending and what we WOULD redeem.
          - Does NOT submit any redeem transactions and does NOT consume
            a relayer quota unit (the underlying call only hits the
            Polymarket data API).
          - ``resolved_at`` is sourced from the position's ``endDate``
            field (ISO-8601 string returned by data-api.polymarket.com
            for resolved markets). When absent or unparseable we leave
            it as ``None`` so the caller can decide how to render
            "unknown overdue".
        """
        rows = await self.fetch_redeemable_positions(outcomes={"WIN"})
        out: list[dict] = []
        for r in rows:
            resolved_at: Optional[datetime] = None
            end_date_raw = r.get("endDate") or r.get("end_date")
            if isinstance(end_date_raw, datetime):
                resolved_at = (
                    end_date_raw if end_date_raw.tzinfo else end_date_raw.replace(tzinfo=timezone.utc)
                )
            elif isinstance(end_date_raw, str) and end_date_raw:
                try:
                    parsed = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                    resolved_at = (
                        parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                    )
                except ValueError:
                    resolved_at = None
            elif isinstance(end_date_raw, (int, float)) and end_date_raw > 0:
                # Polymarket sometimes returns endDate as a unix-millisecond
                # number; fall back to seconds if the value looks small.
                ts = float(end_date_raw)
                if ts > 1e12:
                    ts /= 1000.0
                try:
                    resolved_at = datetime.fromtimestamp(ts, tz=timezone.utc)
                except (OSError, ValueError, OverflowError):
                    resolved_at = None
            value = float(r.get("size", 0.0)) * float(r.get("curPrice", 0.0))
            out.append(
                {
                    "condition_id": r.get("conditionId", ""),
                    "value": value,
                    "resolved_at": resolved_at,
                }
            )
        return out

    async def pending_wins_summary(
        self,
        now: Optional[datetime] = None,
    ) -> tuple[list[dict], bool]:
        """
        Return ``(pending_wins, scan_successful)``.

        Used by the position-snapshot Telegram alert and the Hub
        ``/api/positions/snapshot`` endpoint. Read-only — does NOT trigger
        redemption and does NOT consume a relayer quota unit.

        Output schema (one entry per pending win) matches the
        ``PendingWin`` TypedDict consumed by ``alerts.positions.build_snapshot``::

            {
                "condition_id": str,
                "value": float,            # USD value still locked in the position
                "window_end_utc": str | None,  # ISO-8601 of market resolution
                "overdue_seconds": int,    # seconds since resolution (≥ 0)
            }

        Sort order: descending ``overdue_seconds`` (worst overdue first).
        Positions without a known ``resolved_at`` get ``overdue_seconds=0``
        and float to the bottom of the list.

        **Scan-success contract (audit #204):** on scan failure (data-api
        timeout, 429, network blip) returns ``([], False)`` — the empty
        list means "unknown", NOT "no pending wins". Callers MUST
        propagate the flag to any persistence layer that rewrites the
        pending set, so a transient failure does not wipe the DB-backed
        snapshot (``poly_pending_wins``) and make Daisy think her money
        vanished. Root-caused 2026-04-16 when Wallet $83.31 · Pending
        $112.71 (14 pending) was wiped to 0 pending in 60s after a single
        429-cooldown scan failure, despite the wallet not moving.
        """
        now = now or datetime.now(timezone.utc)
        try:
            positions = await self._scan_redeemable_positions()
        except Exception as exc:
            self._log.warning(
                "redeemer.pending_summary_scan_failed", error=str(exc)[:120]
            )
            return [], False

        out: list[dict] = []
        for p in positions:
            resolved_at = p.get("resolved_at")
            if resolved_at is None:
                overdue = 0
            else:
                overdue = max(0, int((now - resolved_at).total_seconds()))
            out.append(
                {
                    "condition_id": p["condition_id"],
                    "value": float(p.get("value", 0.0)),
                    "window_end_utc": resolved_at.isoformat() if resolved_at else None,
                    "overdue_seconds": overdue,
                }
            )
        # Newest-first → oldest first (worst overdue at the top of the list)
        out.sort(key=lambda x: x["overdue_seconds"], reverse=True)
        return out, True

    def cooldown_status(self) -> dict:
        """Current relayer cooldown + backoff state.

        The base four keys (``active``, ``remaining_seconds``, ``resets_at``,
        ``reason``) describe the server-reported 24 h quota cooldown —
        contract callers have been consuming since 2026-04-10 and must NOT
        change shape.

        Task #196 additively extends this with backoff fields so downstream
        (Hub snapshot row, Telegram position card, FE bar) can surface why
        the redeemer skipped a tick when the base cooldown says "inactive"
        but we're still inside an exponential-backoff window:

          - ``backoff_active``            (bool)
          - ``backoff_remaining_seconds`` (int, 0 when inactive)
          - ``consecutive_429_count``     (int, resets on non-429 success)
        """
        now = datetime.now(timezone.utc)
        backoff_remaining = 0
        backoff_active = self._in_backoff()
        if backoff_active and self._backoff_until is not None:
            backoff_remaining = max(
                0, int((self._backoff_until - now).total_seconds())
            )

        base: dict
        if not self._rate_limit_until:
            base = {
                "active": False,
                "remaining_seconds": 0,
                "resets_at": None,
                "reason": "",
            }
        else:
            base = {
                "active": self._in_cooldown(),
                "remaining_seconds": max(
                    0, int((self._rate_limit_until - now).total_seconds())
                ),
                "resets_at": self._rate_limit_until.isoformat(),
                "reason": self._rate_limit_reason,
            }
        base["backoff_active"] = backoff_active
        base["backoff_remaining_seconds"] = backoff_remaining
        base["consecutive_429_count"] = int(self._consecutive_429)
        return base

    def losses_due(self, interval_hours: int = 24) -> bool:
        if self._last_loss_sweep_at is None:
            return True
        return datetime.now(timezone.utc) - self._last_loss_sweep_at >= timedelta(
            hours=interval_hours
        )

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

        Reset semantics — important: Polymarket's Builder Relayer uses a
        **rolling 24 h quota window**, not a midnight-UTC reset. The 429
        response body includes ``resets in N seconds`` which is the exact
        authoritative countdown from the server's perspective. We parse
        that via ``_parse_reset_seconds`` and trust it literally — there
        is no point computing our own reset time.

        If no reset value can be parsed (malformed error), we fall back
        to ``_DEFAULT_COOLDOWN_SECONDS`` (5 min) which is intentionally
        short so we don't starve ourselves on ambiguous errors. Always
        logs the transition so an operator can see exactly how long the
        cooldown will last.

        Also trips exponential backoff (Task #196). The backoff is
        layered on top of the server cooldown — whichever is longer
        actually gates the next redeem attempt. See ``_trip_backoff``.
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
        # Increment consecutive 429 count + compute next backoff window.
        self._trip_backoff()

    # ── Exponential-backoff helpers (Task #196) ──────────────────────────────
    #
    # The server-reported cooldown (``_rate_limit_until``) tells us WHEN the
    # Polymarket Relayer says our 24h quota will refresh. But even inside
    # that window, each additional redeem call still burns 1 unit and still
    # returns 429. To avoid shaving future-day quota on retries we ALSO
    # track a per-session exponential backoff window.
    #
    # Reset rule: any successful non-429 redeem_position call resets
    # ``_consecutive_429`` to 0 and clears ``_backoff_until``. See
    # ``_clear_backoff_on_success``.

    def _in_backoff(self) -> bool:
        """True iff we are currently inside a backoff window.

        Lazily clears expired state so callers never see stale deadlines.
        """
        if self._backoff_until is None:
            return False
        now = datetime.now(timezone.utc)
        if now >= self._backoff_until:
            # Window elapsed — clear, but keep ``_consecutive_429`` intact.
            # The counter only resets on a SUCCESS, not on mere timeout,
            # so repeated failures keep doubling.
            self._backoff_until = None
            return False
        return True

    def _trip_backoff(self) -> None:
        """Increment the consecutive-429 counter and arm the next backoff.

        Sleep duration = ``min(BASE * 2^(N-1), CAP)`` where N is the NEW
        count after incrementing. So:
          - 1st 429 → 30s
          - 2nd 429 → 60s
          - 3rd 429 → 2m
          - …
          - 7th 429+ → 30m (cap)
        """
        self._consecutive_429 += 1
        # N>=1 here because we just incremented. 2^(N-1) starts at 1.
        sleep_seconds = min(
            _BACKOFF_CAP_SECONDS,
            _BACKOFF_BASE_SECONDS * (2 ** (self._consecutive_429 - 1)),
        )
        now = datetime.now(timezone.utc)
        self._backoff_until = now + timedelta(seconds=sleep_seconds)
        self._log.warning(
            "redeemer.backoff_tripped",
            consecutive_429=self._consecutive_429,
            sleep_seconds=sleep_seconds,
            backoff_until=self._backoff_until.isoformat(),
            cap_reached=(sleep_seconds >= _BACKOFF_CAP_SECONDS),
        )

    def _clear_backoff_on_success(self) -> None:
        """Reset the consecutive-429 counter + clear the backoff window.

        Called from ``redeem_position`` when the relayer returns a non-429
        result (either success or a different error). Keeps the counter
        sticky across timeouts so a real fix is required to reset.
        """
        if self._consecutive_429 > 0 or self._backoff_until is not None:
            self._log.info(
                "redeemer.backoff_cleared",
                had_consecutive_429=self._consecutive_429,
            )
        self._consecutive_429 = 0
        self._backoff_until = None

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
                self._log.warning(
                    "redeemer.no_builder_key", hint="Set BUILDER_KEY env var"
                )
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

    async def fetch_redeemable_positions(
        self,
        outcomes: Optional[set[str]] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
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
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        self._log.warning(
                            "redeemer.positions_api_error", status=resp.status
                        )
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

                row = {
                    "conditionId": condition_id,
                    "size": size,
                    "avgPrice": avg_price,
                    "curPrice": cur_price,
                    "outcome": outcome,
                    "pnl": pnl,
                    "tokenId": p.get("tokenId", ""),
                    "asset": p.get("asset", ""),
                    # Preserve resolution timestamp so _scan_redeemable_positions()
                    # can compute overdue_seconds for the OVERDUE Telegram marker.
                    # Polymarket data-api uses `endDate` (ISO-8601 string); fall
                    # back to `endDateIso` defensively in case the schema shifts.
                    "endDate": p.get("endDate") or p.get("endDateIso"),
                }
                if outcomes is None or outcome in outcomes:
                    redeemable.append(row)

            # Prioritise cash-returning wins and larger-value positions first.
            redeemable.sort(
                key=lambda p: (
                    0 if p["outcome"] == "WIN" else 1,
                    -(p["size"] * p["curPrice"]),
                    -abs(p["pnl"]),
                )
            )
            if limit is not None:
                redeemable = redeemable[:limit]

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

    def _empty_result(self, redeem_type: str) -> dict:
        return {
            "redeem_type": redeem_type,
            "scanned": 0,
            "redeemed": 0,
            "failed": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "total_value": 0.0,
            "usdc_before": 0.0,
            "usdc_after": 0.0,
            "tx_hashes": [],
            "details": [],
            "failed_details": [],
            "paper_mode": self._paper_mode,
        }

    async def _redeem_positions(
        self,
        *,
        positions: list[dict],
        redeem_type: str,
    ) -> dict:
        if self._paper_mode:
            return self._empty_result(redeem_type)
        if self._in_cooldown():
            self._log_cooldown_active()
            result = self._empty_result(redeem_type)
            result.update(self.cooldown_status())
            result["paper_mode"] = False
            return result

        usdc_before = await self.get_usdc_balance()
        if not positions:
            result = self._empty_result(redeem_type)
            result.update(
                {
                    "usdc_before": usdc_before,
                    "usdc_after": usdc_before,
                    "paper_mode": False,
                }
            )
            return result

        redeemed = 0
        failed = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        total_value = 0.0
        details: list[dict] = []
        failed_details: list[dict] = []

        for pos in positions:
            try:
                success = await self.redeem_position(pos["conditionId"])
                details.append(
                    {
                        "conditionId": pos["conditionId"],
                        "outcome": pos["outcome"],
                        "pnl": pos["pnl"],
                        "success": success,
                    }
                )
                if success:
                    redeemed += 1
                    total_pnl += pos["pnl"]
                    total_value += pos["size"] * pos["curPrice"]
                    if pos["outcome"] == "WIN":
                        wins += 1
                    else:
                        losses += 1
                else:
                    failed += 1
                    failed_details.append(
                        {
                            "condition_id": pos.get("conditionId"),
                            "error": "relayer returned non-success",
                        }
                    )
            except Exception as exc:
                self._log.error(
                    "redeemer.sweep_position_error",
                    condition=pos.get("conditionId", "?")[:20],
                    error=str(exc),
                )
                failed += 1
                details.append(
                    {
                        "conditionId": pos.get("conditionId"),
                        "outcome": pos.get("outcome"),
                        "pnl": pos.get("pnl"),
                        "success": False,
                        "error": str(exc)[:120],
                    }
                )
                failed_details.append(
                    {
                        "condition_id": pos.get("conditionId"),
                        "error": str(exc)[:80],
                    }
                )
            await asyncio.sleep(2)

        usdc_after = await self.get_usdc_balance()
        self._log.info(
            "redeemer.sweep_complete",
            redeem_type=redeem_type,
            redeemed=redeemed,
            wins=wins,
            losses=losses,
            failed=failed,
            total_pnl=f"${total_pnl:.2f}",
            usdc_change=f"${usdc_after - usdc_before:.2f}",
        )
        return {
            "redeem_type": redeem_type,
            "scanned": len(positions),
            "redeemed": redeemed,
            "failed": failed,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "total_value": total_value,
            "usdc_before": usdc_before,
            "usdc_after": usdc_after,
            "tx_hashes": [],
            "details": details,
            "failed_details": failed_details,
            "paper_mode": False,
        }

    async def redeem_wins(self, max_positions: int = 2) -> dict:
        positions = await self.fetch_redeemable_positions(
            outcomes={"WIN"}, limit=max_positions
        )
        return await self._redeem_positions(positions=positions, redeem_type="wins")

    async def redeem_losses(self, max_positions: int = 25) -> dict:
        positions = await self.fetch_redeemable_positions(
            outcomes={"LOSS"}, limit=max_positions
        )
        result = await self._redeem_positions(positions=positions, redeem_type="losses")
        self._last_loss_sweep_at = datetime.now(timezone.utc)
        return result

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
            if self._attempts_repo is not None:
                try:
                    await self._attempts_repo.record(
                        condition_id=condition_id,
                        outcome="COOLDOWN",
                        error=self._rate_limit_reason[:200] if self._rate_limit_reason else None,
                    )
                except Exception:
                    pass
            return False

        # ── Backoff gate (Task #196): skip if we're inside an exponential
        # backoff window from a recent 429 but the server-reported
        # cooldown has already drained. Returns False without consuming
        # a relayer quota unit. This is the critical path: before this
        # gate, every tick during cooldown burned an extra 429 unit
        # against tomorrow's quota.
        if self._in_backoff():
            now = datetime.now(timezone.utc)
            remaining = (
                max(0, int((self._backoff_until - now).total_seconds()))
                if self._backoff_until
                else 0
            )
            self._log.info(
                "redeemer.backoff_skip",
                condition=condition_id[:20] + "...",
                remaining_seconds=remaining,
                consecutive_429=self._consecutive_429,
            )
            if self._attempts_repo is not None:
                try:
                    await self._attempts_repo.record(
                        condition_id=condition_id,
                        outcome="BACKOFF",
                        error=f"backoff remaining={remaining}s N={self._consecutive_429}",
                    )
                except Exception:
                    pass
            return False

        # ── Stuck-position gate: skip condition_ids that keep failing ─────
        # Prevents hot-loops on a position that errors every sweep — e.g.
        # chain-level revert, malformed calldata, wrong indexSets. After
        # REDEEM_MAX_FAILURES_24H (default 3) FAILED attempts in 24 h, stop
        # trying until 24 h of silence passes.
        if self._attempts_repo is not None:
            try:
                recent = await self._attempts_repo.recent_failures(
                    condition_id=condition_id, hours=24
                )
                if recent >= self._max_failures_24h:
                    self._log.warning(
                        "redeemer.skip_repeated_failures",
                        condition=condition_id[:20] + "...",
                        recent_failures=recent,
                        threshold=self._max_failures_24h,
                    )
                    return False
            except Exception:
                # Never let the attempts repo crash the sweep.
                pass

        try:
            from web3 import Web3
            from py_builder_relayer_client.models import Transaction

            cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            zero_bytes32 = b"\x00" * 32

            # Build redeemPositions calldata
            fn = self._ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                zero_bytes32,  # parentCollectionId (0 for root collection)
                cid_bytes,  # conditionId
                [1, 2],  # indexSets: YES=1, NO=2
            )
            calldata = fn._encode_transaction_data()

            # Use Transaction (SDK handles Safe vs Proxy based on relay_tx_type)
            txn = Transaction(
                to=CTF_ADDRESS,
                data=calldata,
                value="0",
            )

            # Execute via relay (synchronous SDK call — handles signing internally)
            response = await asyncio.to_thread(self._relay_client.execute, [txn])

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
                20,  # max_polls
                3000,  # poll_frequency ms
            )

            success = bool(confirmed)

            self._log.info(
                "redeemer.redeem_result",
                condition=condition_id[:20] + "...",
                success=success,
                tx_hash=tx_hash,
            )

            # Task #196: any non-429 completion resets the backoff window.
            # Reaching the poll_until_state result means the relayer
            # ACCEPTED the submit (no 429), regardless of whether on-chain
            # settlement confirmed — so treat it as a successful "not
            # rate-limited" exchange with the relayer.
            self._clear_backoff_on_success()

            if self._attempts_repo is not None:
                try:
                    await self._attempts_repo.record(
                        condition_id=condition_id,
                        outcome="SUCCESS" if success else "FAILED",
                        tx_hash=tx_hash,
                        error=None if success else "poll_did_not_confirm",
                    )
                except Exception:
                    pass

            # Mark matching WIN trade rows as redeemed in the canonical
            # trades table. Best-effort: the sweep already happened on-chain
            # (this is just accounting), so a DB hiccup must not surface as
            # a "redemption failed" result.
            if success and self._trades_repo is not None:
                try:
                    updated = await self._trades_repo.mark_redeemed(
                        condition_id=condition_id,
                        tx_hash=tx_hash,
                    )
                    self._log.info(
                        "redeemer.trades_marked_redeemed",
                        condition=condition_id[:20] + "...",
                        rows_updated=updated,
                        tx_hash=tx_hash,
                    )
                except Exception as exc:
                    self._log.warning(
                        "redeemer.trades_mark_redeemed_error",
                        condition=condition_id[:20] + "...",
                        error=str(exc)[:200],
                    )

            return success

        except Exception as exc:
            exc_str = str(exc)
            if _is_rate_limit_error(exc_str):
                # Trip global cooldown — every other pending redemption
                # in this sweep will now short-circuit at `_in_cooldown()`.
                # _trip_cooldown() also increments the backoff counter
                # (Task #196) so repeated 429s double the skip window.
                self._trip_cooldown(exc_str)
                if self._attempts_repo is not None:
                    try:
                        await self._attempts_repo.record(
                            condition_id=condition_id,
                            outcome="COOLDOWN",
                            error=exc_str[:200],
                        )
                    except Exception:
                        pass
            else:
                # Real error, not a rate limit. Log at error level so it
                # triggers alerts; the cooldown path logs at warning.
                self._log.error(
                    "redeemer.redeem_error",
                    condition=condition_id[:20] + "...",
                    error=exc_str[:200],
                )
                if self._attempts_repo is not None:
                    try:
                        await self._attempts_repo.record(
                            condition_id=condition_id,
                            outcome="FAILED",
                            error=exc_str[:500],
                        )
                    except Exception:
                        pass
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
        positions = await self.fetch_redeemable_positions()
        return await self._redeem_positions(positions=positions, redeem_type="all")
