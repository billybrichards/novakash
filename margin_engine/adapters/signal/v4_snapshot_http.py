"""
HTTP polling adapter for /v4/snapshot.

The v4 snapshot endpoint on the timesfm service (novakash-timesfm-repo,
PR #46) returns a rich per-timescale fusion payload: probability_up,
quantile distribution, regime classification, consensus gates, macro
bias, event calendar, cascade FSM state, and cross-timescale alignment —
all in one atomic read.

This adapter polls the endpoint on a fixed cadence (default 2 seconds),
caches the latest response with a short freshness window (default 10
seconds), and exposes the cached V4Snapshot to the use cases via
`get_latest()`. Never raises; always returns None when the cache is
stale, upstream is unreachable, or the payload fails to parse.

Design choices worth calling out:

1. **Fail-soft contract**. On any error (network, HTTP 4xx/5xx, JSON
   parse, missing required fields), the adapter logs a warning and keeps
   the last-successful value. get_latest() returns None once _latest_at
   drifts beyond freshness_s. Never raises. This matches the docstring
   contract on `V4SnapshotPort`.

2. **Eager initial fetch in connect()**. We do one synchronous-from-the-
   caller poll as part of `connect()` so the first `get_latest()` call
   after startup returns a real snapshot rather than None. Mirrors the
   HyperliquidPriceFeed pattern from PR #10.

3. **Asyncio.Event wait loop**. The _poll_loop uses
   `asyncio.wait_for(self._stop.wait(), timeout=poll_interval_s)` rather
   than `asyncio.sleep(poll_interval_s)` so `disconnect()` wakes the
   loop immediately rather than waiting up to poll_interval_s for the
   next sleep tick. Important because systemd may SIGTERM the process
   at any time and we want shutdown to be snappy.

4. **Logging cadence**. Every poll logs at DEBUG. Every successful poll
   that changes the cached mid price logs at INFO at most once per 60
   seconds (sampled). Every failure logs at WARNING. This keeps the log
   stream useful without drowning journalctl.

5. **Dark deploy compatible**. PR A ships this adapter behind the
   `engine_use_v4_actions=False` feature flag — the adapter polls in the
   background and the observation log runs, but no use case actually
   consumes `get_latest()` yet. Flipping the flag in PR B wires the
   adapter into `OpenPositionUseCase` and `ManagePositionsUseCase`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from margin_engine.domain.ports import V4SnapshotPort
from margin_engine.domain.value_objects import V4Snapshot

logger = logging.getLogger(__name__)


class V4SnapshotHttpAdapter(V4SnapshotPort):
    """
    Polls /v4/snapshot on a fixed cadence and caches the latest response.

    Lifecycle:
        adapter = V4SnapshotHttpAdapter(base_url="http://16.52.14.182:8080")
        await adapter.connect()              # starts background poll
        snapshot = await adapter.get_latest()  # returns Optional[V4Snapshot]
        await adapter.disconnect()           # stops poll, closes session
    """

    def __init__(
        self,
        base_url: str = "http://16.52.14.182:8080",
        asset: str = "BTC",
        timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
        strategy: str = "fee_aware_15m",
        poll_interval_s: float = 2.0,
        freshness_s: float = 10.0,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v4/snapshot"
        self._asset = asset.upper()
        self._timescales = tuple(t.strip() for t in timescales if t.strip())
        self._timescales_csv = ",".join(self._timescales)
        self._strategy = strategy
        self._poll_interval_s = poll_interval_s
        self._freshness_s = freshness_s
        self._request_timeout_s = request_timeout_s

        # Cache state
        self._latest: Optional[V4Snapshot] = None
        self._latest_at: float = 0.0
        self._ever_succeeded: bool = False

        # Infra state
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        # Rate-limit INFO logs (one summary per 60 seconds)
        self._last_info_log_at: float = 0.0

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Start the polling loop. Does one synchronous initial fetch first so
        `get_latest()` returns a real value immediately rather than None
        on the first tick after startup.
        """
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout_s),
        )
        # Eager initial fetch — fail soft
        await self._poll_once()
        if not self._ever_succeeded:
            logger.warning(
                "V4SnapshotHttpAdapter: initial fetch failed; use cases that "
                "depend on v4 will see None until a poll succeeds"
            )
        else:
            logger.info(
                "V4SnapshotHttpAdapter connected: %s asset=%s timescales=%s "
                "poll=%.1fs freshness=%.1fs",
                self._url,
                self._asset,
                self._timescales_csv,
                self._poll_interval_s,
                self._freshness_s,
            )

        self._task = asyncio.create_task(
            self._poll_loop(),
            name="v4-snapshot-poll",
        )

    async def disconnect(self) -> None:
        """Stop the polling task, cancel any in-flight request, close session."""
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("V4SnapshotHttpAdapter disconnected")

    # ─── Public read interface ────────────────────────────────────────────

    async def get_latest(
        self,
        asset: str = "BTC",
        timescales: Optional[list[str]] = None,
    ) -> Optional[V4Snapshot]:
        """
        Return the cached snapshot if it's still fresh, else None.

        The `timescales` argument is informational — this adapter always
        returns whatever the poller was configured for. It exists on the
        port signature for forward compatibility with future variants.
        """
        if self._latest is None:
            return None
        if (time.time() - self._latest_at) > self._freshness_s:
            return None
        if asset.upper() != self._latest.asset.upper():
            return None
        return self._latest

    @property
    def is_healthy(self) -> bool:
        """True iff the cache is fresh and we've ever received a snapshot."""
        if self._latest is None:
            return False
        return (time.time() - self._latest_at) <= self._freshness_s

    def info(self) -> dict:
        """
        Lightweight observability payload — safe to call from any context.

        Used by the main loop's periodic observation log (PR A feature)
        to print a one-line summary of the v4 state every N ticks without
        hitting the HTTP endpoint.
        """
        age: Optional[float]
        if self._latest_at > 0:
            age = round(time.time() - self._latest_at, 2)
        else:
            age = None

        snap = self._latest
        if snap is None:
            return {
                "source": "v4_snapshot",
                "healthy": False,
                "last_snapshot_age_s": age,
                "asset": self._asset,
                "ever_succeeded": self._ever_succeeded,
            }

        primary_ts = (
            "15m"
            if "15m" in snap.timescales
            else (next(iter(snap.timescales), None) if snap.timescales else None)
        )
        primary_payload = snap.timescales.get(primary_ts) if primary_ts else None

        return {
            "source": "v4_snapshot",
            "healthy": self.is_healthy,
            "last_snapshot_age_s": age,
            "asset": snap.asset,
            "last_price": snap.last_price,
            "consensus_safe_to_trade": snap.consensus.safe_to_trade,
            "macro_bias": snap.macro.bias,
            "macro_direction_gate": snap.macro.direction_gate,
            "max_impact_in_window": snap.max_impact_in_window,
            "minutes_to_next_high_impact": snap.minutes_to_next_high_impact,
            "primary_ts": primary_ts,
            "primary_status": primary_payload.status if primary_payload else None,
            "primary_regime": primary_payload.regime if primary_payload else None,
            "primary_probability_up": primary_payload.probability_up
            if primary_payload
            else None,
            "primary_expected_move_bps": primary_payload.expected_move_bps
            if primary_payload
            else None,
            "ever_succeeded": self._ever_succeeded,
        }

    # ─── Internals ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Run the poll cadence until disconnect() sets the stop event.

        Uses `asyncio.wait_for(stop.wait(), timeout)` instead of
        `asyncio.sleep()` so shutdown wakes the loop immediately.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._poll_interval_s,
                )
            except asyncio.TimeoutError:
                pass  # expected "next tick" branch
            if self._stop.is_set():
                break

            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Defense in depth — _poll_once handles its own exceptions,
                # but if anything bubbles up (e.g. a bug in the parser),
                # we catch it here so one bad tick doesn't kill the loop.
                logger.warning(
                    "V4SnapshotHttpAdapter poll loop unexpected error: %s",
                    e,
                    exc_info=True,
                )

    async def _poll_once(self) -> None:
        """
        Single HTTP GET to /v4/snapshot.

        Parses the response via V4Snapshot.from_dict and updates the
        cache on success. Logs warnings on any failure but never raises.
        """
        if self._session is None:
            logger.warning("V4SnapshotHttpAdapter: _poll_once called before connect()")
            return

        params = {
            "asset": self._asset,
            "timescales": self._timescales_csv,
            "strategy": self._strategy,
        }

        try:
            async with self._session.get(self._url, params=params) as resp:
                if resp.status != 200:
                    # 503 = assembler not ready; 4xx = client bug; 5xx = upstream
                    body_preview = (await resp.text())[:200]
                    logger.warning(
                        "V4SnapshotHttpAdapter HTTP %d: %s",
                        resp.status,
                        body_preview,
                    )
                    return
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("V4SnapshotHttpAdapter request failed: %s", e)
            return
        except Exception as e:
            logger.warning(
                "V4SnapshotHttpAdapter unexpected network error: %s",
                e,
                exc_info=True,
            )
            return

        # Parse — defensive, never raises on missing fields
        try:
            snap = V4Snapshot.from_dict(payload)
        except KeyError as e:
            # `asset` was missing — the only genuinely required field
            logger.warning(
                "V4SnapshotHttpAdapter: payload missing required field %s",
                e,
            )
            return
        except Exception as e:
            logger.warning(
                "V4SnapshotHttpAdapter: parse failed: %s",
                e,
                exc_info=True,
            )
            return

        self._latest = snap
        self._latest_at = time.time()
        if not self._ever_succeeded:
            self._ever_succeeded = True
            logger.info(
                "V4SnapshotHttpAdapter: first successful poll "
                "(last_price=%s, 15m.status=%s, 15m.regime=%s)",
                snap.last_price,
                snap.timescales.get("15m").status if "15m" in snap.timescales else "?",
                snap.timescales.get("15m").regime if "15m" in snap.timescales else "?",
            )
        else:
            # Sampled INFO log — one line per ~60s with the key state
            now = time.time()
            if now - self._last_info_log_at >= 60:
                self._last_info_log_at = now
                p15 = snap.timescales.get("15m")
                logger.info(
                    "v4 snapshot: last_price=%s consensus_safe=%s macro=%s gate=%s "
                    "max_impact=%s 15m_status=%s 15m_regime=%s 15m_prob=%s",
                    snap.last_price,
                    snap.consensus.safe_to_trade,
                    snap.macro.bias,
                    snap.macro.direction_gate,
                    snap.max_impact_in_window,
                    p15.status if p15 else "?",
                    p15.regime if p15 else "?",
                    f"{p15.probability_up:.3f}" if p15 and p15.probability_up else "?",
                )
