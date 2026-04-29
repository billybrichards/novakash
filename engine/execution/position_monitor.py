"""Post-fill position monitoring and exit system.

After a FAK/FOK buy fills, the PositionMonitor tracks the position and
continues evaluating every 2s (on the existing eval loop cadence). If
the CLOB bid drops below a mark-to-market threshold for N consecutive
ticks, it triggers a sell via the CLOB client.

Architecture:
  - MonitoredPosition: dataclass tracking a single open position
  - PositionMonitor: manages all open positions, evaluates exits,
    executes sells

Integration:
  - on_fill() called from registry.py after ExecuteTradeUseCase succeeds
  - evaluate_exit() called every 2s in the eval loop for each open position
  - execute_exit() places a SELL FAK order on the CLOB to close the position

Exit logic (mark-to-market stop-loss):
  - For UP positions: mark = clob_up_bid (or 1 - clob_down_ask)
  - For DOWN positions: mark = clob_down_bid (or 1 - clob_up_ask)
  - If mark < exit_mark_min_pct * fill_price for exit_mark_ticks
    consecutive ticks, trigger exit.

Safety:
  - exit_shadow_mode: when True, logs exits but does NOT place sell orders
  - exit_eval_start_offset: start checking exits at this T-minus offset
  - exit_eval_end_offset: stop checking exits at this T-minus offset (thin book)
  - exit_max_retries: sell retry count
  - exit_retry_timeout_seconds: per-sell-attempt timeout

Guards (PR #365 + Montreal deploy):
  - Stale position cleanup: positions older than 330s (window + grace) are popped
  - Window match: surface.window_ts must match pos.window_ts

See Hub notes #236 (exit system spec), #298/#299 (audit tasks), #240, #365.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import structlog

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

log = structlog.get_logger(__name__)


@dataclass
class ExitTier:
    """One tier of the multi-tier exit ladder.

    eval_offset is seconds-before-close. A tier is active when
    ``end_offset <= eval_offset <= start_offset`` (i.e. start_offset is
    the *earlier* edge in wall-clock terms, end_offset is the *later*
    edge — naming follows surface.eval_offset convention).
    """

    name: str
    start_offset: int
    end_offset: int
    mark_pct: float  # exit if mark/fill < this for mark_ticks consecutive ticks
    mark_ticks: int


@dataclass
class MonitoredPosition:
    """Tracks a single open position awaiting exit evaluation."""

    strategy_id: str
    window_ts: int
    direction: str  # "UP" or "DOWN"
    fill_price: float
    fill_size: float
    order_id: str
    token_id: str
    filled_at_epoch: float
    confirmed_size: float = 0.0  # CLOB-confirmed fill size (from SOT reconciler)
    consecutive_flip_count: int = 0  # legacy, kept for compat
    mark_loss_tick_count: int = 0
    current_tier_name: str = ""  # active tier name; resets count on transition
    flip_consecutive_count: int = 0  # signal-flip detector (LGB head opposite)
    # ── Hedge-exit state (PR #X, 2026-04-27) ───────────────────────────
    # Buy-opposite-and-hold pattern. ``hedged`` flips to True after a
    # successful opposite-side BUY (or shadow-mode decision) so we don't
    # fire repeatedly on the same position. ``hedge_consensus_count`` is
    # the consecutive-tick counter for the multi-signal gate.
    # ``hedge_token_id_opposite`` carries the OPPOSITE-side CLOB token ID
    # populated at fill-time (registry passes it via on_fill).
    hedge_consensus_count: int = 0
    hedged: bool = False
    hedge_token_id_opposite: str = ""


class PositionMonitor:
    """Monitors open positions and triggers exits when signals flip.

    Designed as a singleton per engine instance. Thread-safe via asyncio
    semantics (single event loop, no locks needed).

    Parameters are read from the strategy's YAML gate_params at call time
    so they can be tuned without code changes.
    """

    def __init__(
        self,
        poly_client: Any = None,
        alerter: Any = None,
        decision_repo: Any = None,
        db_pool: Any = None,
    ) -> None:
        self._positions: dict[str, MonitoredPosition] = {}
        self._poly_client = poly_client
        self._alerter = alerter
        self._decision_repo = decision_repo
        self._db_pool = db_pool  # asyncpg pool for exit_shadow_log writes
        self._log = log.bind(component="position_monitor")

    # ------------------------------------------------------------------
    # Fill registration
    # ------------------------------------------------------------------

    def on_fill(
        self,
        strategy_id: str,
        window_ts: int,
        direction: str,
        fill_price: float,
        fill_size: float,
        order_id: str,
        token_id: str = "",
        confirmed_size: float = 0.0,
        opposite_token_id: str = "",
    ) -> None:
        """Register a new fill for exit monitoring.

        Called from registry.py after a successful LIVE execution.

        Args:
            confirmed_size: CLOB-confirmed fill size from SOT reconciler.
                If 0.0 (unavailable), sell path falls back to
                fill_size * 0.95 (5% haircut safety margin).
            opposite_token_id: CLOB token ID of the opposite outcome.
                Required for hedge-exit (buy-opposite-and-hold) pattern.
                Empty string disables hedge for this position.
        """
        key = f"{strategy_id}:{window_ts}"
        self._positions[key] = MonitoredPosition(
            strategy_id=strategy_id,
            window_ts=window_ts,
            direction=direction,
            fill_price=fill_price,
            fill_size=fill_size,
            order_id=order_id,
            token_id=token_id,
            filled_at_epoch=time.time(),
            confirmed_size=confirmed_size,
            hedge_token_id_opposite=opposite_token_id,
        )
        self._log.info(
            "position_monitor.registered",
            key=key,
            direction=direction,
            fill_price=f"${fill_price:.3f}",
            fill_size=f"{fill_size:.2f}",
        )

    # ------------------------------------------------------------------
    # Exit evaluation
    # ------------------------------------------------------------------

    def evaluate_exit(
        self,
        strategy_id: str,
        window_ts: int,
        surface: "FullDataSurface",
        *,
        exit_monitor_enabled: bool = True,
        exit_eval_start_offset: int = 48,
        exit_eval_end_offset: int = 30,
        exit_mark_min_pct: float = 0.45,
        exit_mark_ticks: int = 10,
        # Multi-tier ladder (PR #402, 2026-04-27). When provided, replaces
        # the single-tier legacy gate above. Each tier:
        #   { name, start_offset, end_offset, mark_pct, mark_ticks }
        exit_tiers: Optional[list] = None,
        # Signal-flip detector (PR #402). LGB head opposite-direction strong
        # signal for N consecutive ticks → exit. v1 is exit-only; reversal
        # re-entry deferred (requires schema change).
        flip_enabled: bool = False,
        flip_p_threshold: float = 0.85,
        flip_dist_threshold: float = 0.20,
        flip_consecutive_ticks: int = 3,
        flip_min_offset: int = 60,
        flip_max_offset: int = 240,
        # Stale mark guard
        stale_mark_max_age_seconds: float = 5.0,
        # Legacy params (ignored, kept for call-site compat during rollout)
        exit_min_hold_seconds: int = 45,
        exit_no_exit_last_seconds: int = 30,
        exit_consecutive_flip_ticks: int = 5,
        exit_lgb_flip_enabled: bool = False,
        exit_oracle_flip_enabled: bool = False,
    ) -> Optional[str]:
        """Check whether a monitored position should exit via mark-to-market.

        Called every 2s from the strategy eval loop. Uses CLOB bid prices
        to determine if the position has lost too much value. If the
        mark-to-market ratio (current bid / fill price) stays below
        exit_mark_min_pct for exit_mark_ticks consecutive evaluations,
        triggers a stop-loss exit.

        Args:
            strategy_id: Strategy that owns the position.
            window_ts: Window timestamp of the position.
            surface: Current FullDataSurface with CLOB bid/ask data.
            exit_monitor_enabled: Master switch for exit monitoring.
            exit_eval_start_offset: Start checking exits at this T-minus offset.
            exit_eval_end_offset: Stop checking exits at this T-minus offset.
            exit_mark_min_pct: Exit if bid < this fraction of fill price.
            exit_mark_ticks: Number of consecutive ticks below threshold to trigger.
            exit_min_hold_seconds: Legacy (unused, kept for call-site compat).
            exit_no_exit_last_seconds: Legacy (unused, kept for call-site compat).
            exit_consecutive_flip_ticks: Legacy (unused).
            exit_lgb_flip_enabled: Legacy (unused).
            exit_oracle_flip_enabled: Legacy (unused).

        Returns:
            Exit reason string or None.
        """
        if not exit_monitor_enabled:
            return None

        key = f"{strategy_id}:{window_ts}"
        pos = self._positions.get(key)
        if pos is None:
            return None

        # ── Stale position guard ────────────────────────────────────────
        # Window duration (300s) + grace (30s) = 330s. If the position is
        # older than this, the window has long since resolved — clean up.
        now = time.time()
        if now - pos.filled_at_epoch > 330:
            self._log.info(
                "position_monitor.stale_cleanup",
                key=key,
                held_seconds=f"{now - pos.filled_at_epoch:.0f}",
            )
            self._positions.pop(key, None)
            return None

        # ── Window match guard ──────────────────────────────────────────
        # Surface may be for a different window than the position we're
        # evaluating. Skip if window_ts doesn't match.
        surface_window_ts = getattr(surface, "window_ts", None)
        if surface_window_ts is not None and surface_window_ts != pos.window_ts:
            return None

        # ── Resolve tier ladder ────────────────────────────────────────
        # If exit_tiers provided, use ladder. Else synthesize legacy single
        # tier from exit_eval_*/exit_mark_* params (backwards-compat).
        tiers = self._resolve_tiers(
            exit_tiers,
            exit_eval_start_offset,
            exit_eval_end_offset,
            exit_mark_min_pct,
            exit_mark_ticks,
        )

        eval_offset = getattr(surface, "eval_offset", None)
        if eval_offset is None:
            return None

        current_tier = self._find_current_tier(tiers, eval_offset)

        # ── Stale mark guard ───────────────────────────────────────────
        # If CLOB feed is stale, don't increment loss counter — a feed
        # outage could otherwise fake-trigger every tier sequentially.
        last_clob = getattr(surface, "last_clob_update_ts", None)
        if last_clob is not None:
            try:
                if (now - float(last_clob)) > stale_mark_max_age_seconds:
                    self._log.debug(
                        "position_monitor.stale_mark_skip",
                        key=key,
                        age=f"{now - float(last_clob):.1f}s",
                    )
                    return None
            except (TypeError, ValueError):
                pass

        # Mark-to-market: get current bid for our token
        if pos.direction == "UP":
            up_bid = getattr(surface, "clob_up_bid", None)
            dn_ask = getattr(surface, "clob_down_ask", None)
            if up_bid is not None and float(up_bid) > 0.01:
                mark = float(up_bid)
            elif dn_ask is not None:
                mark = 1.0 - float(dn_ask)
            else:
                return None  # can't price, skip
        else:  # DOWN
            dn_bid = getattr(surface, "clob_down_bid", None)
            up_ask = getattr(surface, "clob_up_ask", None)
            if dn_bid is not None and float(dn_bid) > 0.01:
                mark = float(dn_bid)
            elif up_ask is not None:
                mark = 1.0 - float(up_ask)
            else:
                return None  # can't price, skip

        mark_pct = mark / pos.fill_price if pos.fill_price > 0 else 1.0

        # ── Tier-based stop-loss ───────────────────────────────────────
        if current_tier is not None:
            # Reset counter on tier transition (each tier has its own
            # threshold — counts aren't comparable across tiers).
            if pos.current_tier_name != current_tier.name:
                pos.mark_loss_tick_count = 0
                pos.current_tier_name = current_tier.name

            if mark_pct < current_tier.mark_pct:
                pos.mark_loss_tick_count += 1
            else:
                pos.mark_loss_tick_count = 0

            if pos.mark_loss_tick_count >= current_tier.mark_ticks:
                return (
                    f"{current_tier.name}_mark_stop_loss: "
                    f"mark={mark:.3f} ({mark_pct:.0%} of fill) "
                    f"for {pos.mark_loss_tick_count} ticks "
                    f"@ T-{int(eval_offset)}"
                )
        else:
            # Outside any tier — don't drop the count (preserve state for
            # next active-tier window).
            pass

        # ── Signal-flip detector ───────────────────────────────────────
        if (
            flip_enabled
            and flip_min_offset <= eval_offset <= flip_max_offset
        ):
            opposite = "DOWN" if pos.direction == "UP" else "UP"
            lgb_p_up = getattr(surface, "lgb_p_up", None) or getattr(surface, "probability_lgb", None)
            lgb_dist = getattr(surface, "lgb_dist", None) or getattr(surface, "poly_confidence_distance", None)
            if lgb_p_up is not None and lgb_dist is not None:
                try:
                    p_up_f = float(lgb_p_up)
                    dist_f = float(lgb_dist)
                    p_opp = (1.0 - p_up_f) if opposite == "DOWN" else p_up_f
                    if (
                        p_opp >= flip_p_threshold
                        and dist_f >= flip_dist_threshold
                    ):
                        pos.flip_consecutive_count += 1
                    else:
                        pos.flip_consecutive_count = 0

                    if pos.flip_consecutive_count >= flip_consecutive_ticks:
                        return (
                            f"signal_flip: lgb_p_{opposite.lower()}={p_opp:.2f} "
                            f"dist={dist_f:.2f} for "
                            f"{pos.flip_consecutive_count} ticks "
                            f"@ T-{int(eval_offset)}"
                        )
                except (TypeError, ValueError):
                    pass

        return None

    @staticmethod
    def _resolve_tiers(
        exit_tiers: Optional[list],
        legacy_start: int,
        legacy_end: int,
        legacy_pct: float,
        legacy_ticks: int,
    ) -> list[ExitTier]:
        """Parse YAML tier dicts to ExitTier list, or synth legacy tier.

        Backwards-compat: if no exit_tiers provided, synthesize a single
        tier from the legacy exit_eval_*/exit_mark_* gate_params. This
        keeps unmigrated strategies behaving identically.
        """
        if exit_tiers:
            out: list[ExitTier] = []
            for t in exit_tiers:
                try:
                    out.append(
                        ExitTier(
                            name=str(t.get("name", "tier")),
                            start_offset=int(t["start_offset"]),
                            end_offset=int(t["end_offset"]),
                            mark_pct=float(t["mark_pct"]),
                            mark_ticks=int(t["mark_ticks"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    # Skip malformed tier rather than fail open
                    continue
            if out:
                return out
        # Legacy fallback
        return [
            ExitTier(
                name="legacy",
                start_offset=int(legacy_start),
                end_offset=int(legacy_end),
                mark_pct=float(legacy_pct),
                mark_ticks=int(legacy_ticks),
            )
        ]

    @staticmethod
    def _find_current_tier(
        tiers: list[ExitTier], eval_offset: float
    ) -> Optional[ExitTier]:
        """Return the active tier for eval_offset, or None if in a gap."""
        try:
            offset = float(eval_offset)
        except (TypeError, ValueError):
            return None
        for t in tiers:
            if t.end_offset <= offset <= t.start_offset:
                return t
        return None

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    async def execute_exit(
        self,
        strategy_id: str,
        window_ts: int,
        reason: str,
        *,
        exit_shadow_mode: bool = True,
        exit_max_retries: int = 1,
        exit_retry_timeout_seconds: int = 5,
    ) -> bool:
        """Sell the position on CLOB.

        In shadow mode (default), logs the exit but does NOT place a sell
        order. This lets operators validate exit signals before enabling
        real sells.

        Args:
            strategy_id: Strategy that owns the position.
            window_ts: Window timestamp.
            reason: Exit reason string.
            exit_shadow_mode: If True, log only (don't sell).
            exit_max_retries: Number of sell retries.
            exit_retry_timeout_seconds: Timeout per sell attempt.

        Returns:
            True if exit was executed (or shadow-logged), False if no position found.
        """
        key = f"{strategy_id}:{window_ts}"
        pos = self._positions.pop(key, None)  # pop IMMEDIATELY — prevents double-entry
        if pos is None:
            return False

        held_seconds = time.time() - pos.filled_at_epoch

        if exit_shadow_mode:
            self._log.info(
                "position_monitor.exit_shadow",
                strategy_id=strategy_id,
                window_ts=window_ts,
                direction=pos.direction,
                fill_price=f"${pos.fill_price:.3f}",
                fill_size=f"{pos.fill_size:.2f}",
                held_seconds=f"{held_seconds:.0f}",
                reason=reason,
            )
            # Record shadow exit decision
            await self._record_exit_decision(
                pos, reason, executed=False, shadow=True
            )
            # Write to exit_shadow_log (fire-and-forget)
            _det = "flip" if "signal_flip" in reason else (
                "mark_stop" if "mark_stop" in reason else (
                    "fade" if "conviction_fade" in reason else "tier"
                )
            )
            await self._write_exit_shadow_log(
                strategy_id=strategy_id,
                window_ts=window_ts,
                direction=pos.direction,
                detector_type=_det,
                entry_price=pos.fill_price,
                stake_usd=pos.fill_price * pos.fill_size,
                consecutive_ticks=pos.mark_loss_tick_count if "mark_stop" in reason else pos.flip_consecutive_count,
                triggered=True,
                shadow_mode=True,
                reason=reason,
            )
            # Send TG alert for shadow exit
            await self._send_exit_alert(
                pos, reason, executed=False, shadow=True
            )
            return True

        # Real exit: place SELL FAK order
        sell_success = False
        for attempt in range(1, exit_max_retries + 1):
            try:
                sell_success = await self._place_sell_order(
                    pos, timeout=exit_retry_timeout_seconds
                )
                if sell_success:
                    break
            except Exception as exc:
                self._log.warning(
                    "position_monitor.sell_attempt_failed",
                    attempt=attempt,
                    max_retries=exit_max_retries,
                    error=str(exc)[:200],
                )

        # Record and alert
        await self._record_exit_decision(
            pos, reason, executed=sell_success, shadow=False
        )
        # Write to exit_shadow_log (live exit)
        _det_live = "flip" if "signal_flip" in reason else (
            "mark_stop" if "mark_stop" in reason else (
                "fade" if "conviction_fade" in reason else "tier"
            )
        )
        await self._write_exit_shadow_log(
            strategy_id=strategy_id,
            window_ts=window_ts,
            direction=pos.direction,
            detector_type=_det_live,
            entry_price=pos.fill_price,
            stake_usd=pos.fill_price * pos.fill_size,
            triggered=True,
            shadow_mode=False,
            reason=reason,
        )
        await self._send_exit_alert(
            pos, reason, executed=sell_success, shadow=False
        )

        return sell_success

    async def _place_sell_order(
        self,
        pos: MonitoredPosition,
        timeout: int = 5,
    ) -> bool:
        """Place a SELL FAK order on the CLOB to close the position.

        Uses the same CLOB client as buy-side but with side=SELL.
        """
        if self._poly_client is None:
            self._log.warning(
                "position_monitor.no_poly_client",
                order_id=pos.order_id[:20],
            )
            return False

        if not pos.token_id:
            self._log.warning(
                "position_monitor.no_token_id",
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
            )
            return False

        try:
            # Price: sell at market (use a low limit to ensure fill)
            # For binary tokens: sell YES at any price above 0.01
            # The py_clob_client supports SELL side via the same OrderArgs
            sell_price = 0.01  # aggressive: take any bid, we want OUT fast
            sell_size = pos.confirmed_size if pos.confirmed_size > 0 else round(pos.fill_size * 0.95, 3)

            # Round size to 3dp to match CLOB precision
            sell_size = round(sell_size, 3)
            if sell_size <= 0:
                return False

            result = await self._poly_client.place_sell_fak(
                token_id=pos.token_id,
                price=sell_price,
                size=sell_size,
            )

            filled = result.get("filled", False)
            self._log.info(
                "position_monitor.sell_result",
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
                filled=filled,
                sell_price=f"${sell_price:.4f}",
                size_matched=result.get("size_matched", 0),
                order_id=result.get("order_id", "none")[:20],
            )
            return filled

        except Exception as exc:
            self._log.error(
                "position_monitor.sell_error",
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
                error=str(exc)[:200],
            )
            return False

    async def _write_exit_shadow_log(
        self,
        *,
        strategy_id: str,
        window_ts: int,
        direction: str,
        detector_type: str,
        entry_dist: Optional[float] = None,
        entry_price: Optional[float] = None,
        stake_usd: Optional[float] = None,
        current_dist: Optional[float] = None,
        current_p_up: Optional[float] = None,
        fade_pct: Optional[float] = None,
        eval_offset: Optional[int] = None,
        consecutive_ticks: Optional[int] = None,
        triggered: bool = False,
        shadow_mode: bool = True,
        reason: Optional[str] = None,
    ) -> None:
        """Fire-and-forget INSERT into exit_shadow_log.

        Swallows all errors — never crashes the monitor on DB failure.
        Falls back to log-only when no DB pool is available.
        """
        if self._db_pool is None:
            self._log.debug(
                "exit_shadow_log.no_pool",
                strategy_id=strategy_id,
                detector_type=detector_type,
            )
            return
        try:
            await self._db_pool.execute(
                """
                INSERT INTO exit_shadow_log (
                    strategy_id, window_ts, direction, detector_type,
                    entry_dist, entry_price, stake_usd,
                    current_dist, current_p_up, fade_pct,
                    eval_offset, consecutive_ticks,
                    triggered, shadow_mode, reason
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                strategy_id,
                window_ts,
                direction,
                detector_type,
                entry_dist,
                entry_price,
                stake_usd,
                current_dist,
                current_p_up,
                fade_pct,
                eval_offset,
                consecutive_ticks,
                triggered,
                shadow_mode,
                reason,
            )
        except Exception as exc:
            self._log.debug(
                "exit_shadow_log.write_error",
                error=str(exc)[:200],
            )

    async def _record_exit_decision(
        self,
        pos: MonitoredPosition,
        reason: str,
        executed: bool,
        shadow: bool,
    ) -> None:
        """Write EXIT decision to strategy_decisions table."""
        if self._decision_repo is None:
            return
        try:
            import json

            from domain.value_objects import StrategyDecisionRecord

            record = StrategyDecisionRecord(
                strategy_id=pos.strategy_id,
                strategy_version="exit-monitor-1.0",
                asset="BTC",
                window_ts=pos.window_ts,
                timeframe="5m",
                eval_offset=0,
                mode="SHADOW" if shadow else "LIVE",
                action="EXIT",
                direction=pos.direction,
                confidence=None,
                confidence_score=None,
                entry_cap=None,
                collateral_pct=None,
                entry_reason="",
                skip_reason=None,
                metadata_json=json.dumps({
                    "exit_reason": reason,
                    "executed": executed,
                    "shadow": shadow,
                    "fill_price": pos.fill_price,
                    "fill_size": pos.fill_size,
                    "order_id": pos.order_id,
                    "held_seconds": time.time() - pos.filled_at_epoch,
                    "mark_loss_tick_count": pos.mark_loss_tick_count,
                }),
                evaluated_at=time.time(),
            )

            await self._decision_repo.write_decision(record)
        except Exception as exc:
            self._log.warning(
                "position_monitor.record_error",
                error=str(exc)[:200],
            )

    async def _send_exit_alert(
        self,
        pos: MonitoredPosition,
        reason: str,
        executed: bool,
        shadow: bool,
    ) -> None:
        """Send Telegram alert for exit event."""
        if self._alerter is None:
            return

        held_secs = time.time() - pos.filled_at_epoch
        mode = "SHADOW" if shadow else ("EXECUTED" if executed else "FAILED")
        emoji = {"SHADOW": "\U0001f441\ufe0f", "EXECUTED": "\U0001f4b0", "FAILED": "\u274c"}.get(mode, "\u2753")

        msg = (
            f"{emoji} *EXIT {mode}* — {pos.strategy_id}\n"
            f"direction: `{pos.direction}` window: `{pos.window_ts}`\n"
            f"fill: `${pos.fill_price:.3f}` size: `{pos.fill_size:.2f}`\n"
            f"held: `{held_secs:.0f}s` mark_ticks: `{pos.mark_loss_tick_count}`\n"
            f"reason: {reason}"
        )

        try:
            send = getattr(self._alerter, "send_raw_message", None)
            if send is not None:
                await send(msg)
            elif hasattr(self._alerter, "send_system_alert"):
                await self._alerter.send_system_alert(msg)
        except Exception as exc:
            self._log.warning(
                "position_monitor.alert_error",
                error=str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # Hedge exit (buy-opposite-and-hold) — PR #X, 2026-04-27
    # ------------------------------------------------------------------
    #
    # Mathematically equivalent to ``mergePositions`` on the CTF, but
    # simpler — no smart-contract calldata, just an FAK BUY for the
    # OPPOSITE token at the ask. Both sides held to settlement; the
    # winning side is auto-redeemed by the existing redeemer flow.
    #
    # Net P&L per share (when fired):
    #   1.0  -  our_fill  -  opposite_ask   (guaranteed if both fill)
    #
    # Why this works: Polymarket's bid side is a $0.01 stub on losing
    # outcomes (no real depth above the floor), so FAK-sell exits leak
    # ~$13/trade. Asks on the opposite side ARE real (traders quoting
    # to win), so a BUY there pays the real price. The arithmetic
    # below picks only positions where the BUY price plus our entry
    # leaves a meaningful guaranteed profit floor.

    def evaluate_hedge_exit(
        self,
        strategy_id: str,
        window_ts: int,
        surface: "FullDataSurface",
        *,
        hedge_exit_enabled: bool = False,
        hedge_lgb_p_opposite_min: float = 0.85,
        hedge_lgb_dist_min: float = 0.20,
        hedge_chainlink_delta_opposite: bool = True,
        hedge_tiingo_delta_opposite: bool = True,
        hedge_consensus_consecutive_ticks: int = 5,
        hedge_active_offset_min: int = 90,
        hedge_active_offset_max: int = 200,
        hedge_max_opposite_ask: float = 0.45,
        hedge_min_guaranteed_profit_usd: float = 0.50,
        stale_mark_max_age_seconds: float = 5.0,
    ) -> Optional[dict]:
        """Check whether the multi-signal hedge gate has fired.

        Returns a hedge-instruction dict (with opposite_token_id, size,
        max_buy_price, expected_guaranteed_profit) when ALL of:

          1. ``hedge_exit_enabled`` is True
          2. position is found, not already hedged, and has an
             opposite-side token id
          3. eval_offset ∈ [min, max] (the real-liquidity zone)
          4. CLOB feed is fresh (< ``stale_mark_max_age_seconds`` old)
          5. multi-signal consensus (LGB direction-head AND Chainlink
             delta AND Tiingo delta all point AGAINST our position) holds
             for ``hedge_consensus_consecutive_ticks`` consecutive ticks
          6. economic gate: opposite_ask ≤ ``hedge_max_opposite_ask`` AND
             guaranteed_profit_total ≥ ``hedge_min_guaranteed_profit_usd``

        Returns None otherwise. Fail-open behaviour: any missing field
        on the surface (LGB heads, deltas, opposite ask) returns None
        — never fires on partial data.
        """
        if not hedge_exit_enabled:
            return None

        key = f"{strategy_id}:{window_ts}"
        pos = self._positions.get(key)
        if pos is None:
            return None
        if pos.hedged:
            return None
        if not pos.hedge_token_id_opposite:
            # Without an opposite token id we cannot place the BUY. Log
            # once-per-position would be ideal but we re-evaluate every
            # tick; a debug log keeps noise down.
            self._log.debug(
                "position_monitor.hedge_skip_no_opposite_token",
                key=key,
            )
            return None

        # ── Window match guard ────────────────────────────────────────
        surface_window_ts = getattr(surface, "window_ts", None)
        if surface_window_ts is not None and surface_window_ts != pos.window_ts:
            return None

        # ── Active offset window ──────────────────────────────────────
        eval_offset = getattr(surface, "eval_offset", None)
        if eval_offset is None:
            return None
        try:
            offset_f = float(eval_offset)
        except (TypeError, ValueError):
            return None
        if not (hedge_active_offset_min <= offset_f <= hedge_active_offset_max):
            # Outside hedge window — preserve count for next tick (don't
            # zero it: a flicker outside the window shouldn't reset a
            # nearly-fired gate).
            return None

        # ── Stale CLOB guard ──────────────────────────────────────────
        now = time.time()
        last_clob = getattr(surface, "last_clob_update_ts", None)
        if last_clob is not None:
            try:
                if (now - float(last_clob)) > stale_mark_max_age_seconds:
                    return None
            except (TypeError, ValueError):
                pass

        # Direction normalization. Positions store "UP"/"DOWN".
        our_dir = (pos.direction or "").upper()
        if our_dir not in ("UP", "DOWN"):
            return None
        opposite = "DOWN" if our_dir == "UP" else "UP"

        # ── LGB consensus (model says opposite is strongly favored) ───
        # Pick the right LGB field for the strategy. v9_lgb_only reads
        # ``lgb_p_up`` (alias of probability_lgb on the surface for tests
        # / shadow logging); v10 strategies read ``lgb_p_up_v10``. Try
        # both, prefer the v10-suffixed one when the strategy id contains
        # "v10" so the right field wins.
        lgb_p_up_v10 = getattr(surface, "lgb_p_up_v10", None)
        lgb_p_up = getattr(surface, "lgb_p_up", None)
        # Fallback to the canonical surface fields when the *_p_up
        # aliases aren't populated (shadow tests use them; live surface
        # exposes probability_lgb / probability_lgb_v10).
        if lgb_p_up is None:
            lgb_p_up = getattr(surface, "probability_lgb", None)
        if lgb_p_up_v10 is None:
            lgb_p_up_v10 = getattr(surface, "probability_lgb_v10", None)
        if "v10" in (strategy_id or "").lower():
            chosen_p_up = lgb_p_up_v10 if lgb_p_up_v10 is not None else lgb_p_up
        else:
            chosen_p_up = lgb_p_up if lgb_p_up is not None else lgb_p_up_v10
        lgb_dist = getattr(surface, "lgb_dist", None) or getattr(surface, "poly_confidence_distance", None)
        if chosen_p_up is None or lgb_dist is None:
            # Surface incomplete — fail closed.
            pos.hedge_consensus_count = 0
            return None
        try:
            p_up_f = float(chosen_p_up)
            dist_f = float(lgb_dist)
        except (TypeError, ValueError):
            pos.hedge_consensus_count = 0
            return None
        p_opp = (1.0 - p_up_f) if opposite == "DOWN" else p_up_f
        lgb_pass = (
            p_opp >= hedge_lgb_p_opposite_min and dist_f >= hedge_lgb_dist_min
        )

        # ── Oracle-delta consensus ────────────────────────────────────
        # Sign opposite our direction = "delta points against us".
        # For UP positions we want a NEGATIVE delta to count against us.
        chainlink_delta = getattr(surface, "chainlink_delta", None)
        if chainlink_delta is None:
            chainlink_delta = getattr(surface, "delta_chainlink", None)
        tiingo_delta = getattr(surface, "tiingo_delta", None)
        if tiingo_delta is None:
            tiingo_delta = getattr(surface, "delta_tiingo", None)

        def _delta_against(d: Any) -> Optional[bool]:
            if d is None:
                return None
            try:
                df = float(d)
            except (TypeError, ValueError):
                return None
            if our_dir == "UP":
                return df < 0.0
            return df > 0.0

        chainlink_against = _delta_against(chainlink_delta)
        tiingo_against = _delta_against(tiingo_delta)

        chainlink_pass = (not hedge_chainlink_delta_opposite) or (
            chainlink_against is True
        )
        tiingo_pass = (not hedge_tiingo_delta_opposite) or (
            tiingo_against is True
        )

        # If a required delta source is missing, treat as fail (no consensus).
        if hedge_chainlink_delta_opposite and chainlink_against is None:
            pos.hedge_consensus_count = 0
            return None
        if hedge_tiingo_delta_opposite and tiingo_against is None:
            pos.hedge_consensus_count = 0
            return None

        consensus = lgb_pass and chainlink_pass and tiingo_pass

        if consensus:
            pos.hedge_consensus_count += 1
        else:
            pos.hedge_consensus_count = 0

        if pos.hedge_consensus_count < hedge_consensus_consecutive_ticks:
            return None

        # ── Economic gate ─────────────────────────────────────────────
        # Opposite ask is what we'd pay per share. UP position →
        # opposite is DOWN, so look at clob_down_ask. And vice versa.
        if our_dir == "UP":
            opposite_ask = getattr(surface, "clob_down_ask", None)
        else:
            opposite_ask = getattr(surface, "clob_up_ask", None)
        if opposite_ask is None:
            return None
        try:
            opp_ask_f = float(opposite_ask)
        except (TypeError, ValueError):
            return None
        if opp_ask_f <= 0.0:
            return None
        if opp_ask_f > hedge_max_opposite_ask:
            self._log.info(
                "position_monitor.hedge_economic_skip_high_ask",
                key=key,
                opposite_ask=f"${opp_ask_f:.4f}",
                cap=f"${hedge_max_opposite_ask:.4f}",
            )
            return None

        size = pos.confirmed_size if pos.confirmed_size > 0 else pos.fill_size
        if size <= 0:
            return None

        guaranteed_per_share = 1.0 - pos.fill_price - opp_ask_f
        guaranteed_total = guaranteed_per_share * size
        if guaranteed_total < hedge_min_guaranteed_profit_usd:
            self._log.info(
                "position_monitor.hedge_economic_skip_low_profit",
                key=key,
                guaranteed_total=f"${guaranteed_total:.3f}",
                min_floor=f"${hedge_min_guaranteed_profit_usd:.3f}",
            )
            return None

        # Slip buffer: we want to actually fill, so allow paying up to
        # (current_ask + 0.02) but never above the configured cap. This
        # lets us cross a thin book without overpaying.
        max_buy_price = min(opp_ask_f + 0.02, float(hedge_max_opposite_ask))

        return {
            "opposite_token_id": pos.hedge_token_id_opposite,
            "size_to_buy": float(size),
            "max_buy_price": float(max_buy_price),
            "expected_guaranteed_profit": float(guaranteed_total),
            "opposite_ask": float(opp_ask_f),
            "fill_price": float(pos.fill_price),
            "consensus_ticks": int(pos.hedge_consensus_count),
            "eval_offset": int(offset_f),
            "direction_we_held": our_dir,
            "opposite_direction": opposite,
        }

    async def execute_hedge_exit(
        self,
        strategy_id: str,
        window_ts: int,
        instruction: dict,
        *,
        hedge_shadow_mode: bool = True,
        hedge_max_retries: int = 1,
        hedge_buy_timeout_seconds: int = 5,
    ) -> bool:
        """Place an FAK BUY for the OPPOSITE token to hedge the position.

        Critical: we DO NOT pop ``pos`` from ``_positions`` after a
        successful buy. The position stays "hedged"; both sides are held
        to settlement and the winning side is auto-redeemed by the
        existing redeemer. We only set ``pos.hedged=True`` so the gate
        won't refire.

        Shadow mode logs the decision + counterfactual and flips
        ``pos.hedged=True`` so we don't churn alerts. Real mode places
        the buy via ``poly_client.place_market_order`` (FAK). On a
        single-attempt failure we retry up to ``hedge_max_retries`` times
        before alerting ops; ``pos.hedged`` stays False so the gate is
        free to refire on the next tick if conditions still hold.
        """
        key = f"{strategy_id}:{window_ts}"
        pos = self._positions.get(key)
        if pos is None:
            return False

        opposite_token_id = instruction.get("opposite_token_id") or pos.hedge_token_id_opposite
        size_to_buy = float(instruction.get("size_to_buy", pos.fill_size))
        max_buy_price = float(instruction.get("max_buy_price", 0.0))
        guaranteed_total = float(instruction.get("expected_guaranteed_profit", 0.0))

        if hedge_shadow_mode:
            self._log.info(
                "position_monitor.hedge.shadow_decision",
                strategy_id=strategy_id,
                window_ts=window_ts,
                direction=pos.direction,
                fill_price=f"${pos.fill_price:.4f}",
                opposite_ask=f"${instruction.get('opposite_ask', 0.0):.4f}",
                size=f"{size_to_buy:.3f}",
                max_buy_price=f"${max_buy_price:.4f}",
                guaranteed_total=f"${guaranteed_total:.3f}",
                consensus_ticks=instruction.get("consensus_ticks"),
                eval_offset=instruction.get("eval_offset"),
            )
            pos.hedged = True
            await self._record_hedge_decision(
                pos, instruction, executed=False, shadow=True
            )
            # Write to exit_shadow_log (hedge shadow)
            await self._write_exit_shadow_log(
                strategy_id=strategy_id,
                window_ts=window_ts,
                direction=pos.direction,
                detector_type="hedge",
                entry_price=pos.fill_price,
                stake_usd=pos.fill_price * pos.fill_size,
                eval_offset=instruction.get("eval_offset"),
                consecutive_ticks=instruction.get("consensus_ticks"),
                triggered=True,
                shadow_mode=True,
                reason=f"hedge: guaranteed=${guaranteed_total:.3f} opp_ask=${instruction.get('opposite_ask', 0.0):.4f}",
            )
            await self._send_hedge_alert(
                pos, instruction, executed=False, shadow=True
            )
            return True

        if self._poly_client is None:
            self._log.warning(
                "position_monitor.hedge.no_poly_client",
                strategy_id=strategy_id,
                window_ts=window_ts,
            )
            return False
        if not opposite_token_id:
            self._log.warning(
                "position_monitor.hedge.no_opposite_token",
                strategy_id=strategy_id,
                window_ts=window_ts,
            )
            return False

        # Real mode: FAK BUY of opposite token at the limit price.
        buy_success = False
        size_matched = 0.0
        order_id: Optional[str] = None
        for attempt in range(1, max(1, int(hedge_max_retries)) + 1):
            try:
                # Reuse existing buy primitive (FAK). place_market_order
                # is the canonical BUY path used by the entry ladder; we
                # call it directly with order_type=FAK to mirror the
                # exit-side place_sell_fak behaviour.
                result = await asyncio.wait_for(
                    self._poly_client.place_market_order(
                        token_id=opposite_token_id,
                        price=float(max_buy_price),
                        size=float(size_to_buy),
                        order_type="FAK",
                    ),
                    timeout=float(hedge_buy_timeout_seconds),
                )
                buy_success = bool(result.get("filled", False))
                size_matched = float(result.get("size_matched", 0.0) or 0.0)
                order_id = result.get("order_id")
                if buy_success:
                    break
            except asyncio.TimeoutError:
                self._log.warning(
                    "position_monitor.hedge.buy_timeout",
                    attempt=attempt,
                    timeout=hedge_buy_timeout_seconds,
                )
            except Exception as exc:
                self._log.warning(
                    "position_monitor.hedge.buy_attempt_failed",
                    attempt=attempt,
                    max_retries=hedge_max_retries,
                    error=str(exc)[:200],
                )

        if buy_success:
            pos.hedged = True

        # Augment instruction with execution result for downstream record.
        result_instruction = dict(instruction)
        result_instruction["size_matched"] = size_matched
        result_instruction["order_id"] = order_id

        await self._record_hedge_decision(
            pos, result_instruction, executed=buy_success, shadow=False
        )
        # Write to exit_shadow_log (hedge live)
        await self._write_exit_shadow_log(
            strategy_id=strategy_id,
            window_ts=window_ts,
            direction=pos.direction,
            detector_type="hedge",
            entry_price=pos.fill_price,
            stake_usd=pos.fill_price * pos.fill_size,
            eval_offset=instruction.get("eval_offset"),
            consecutive_ticks=instruction.get("consensus_ticks"),
            triggered=True,
            shadow_mode=False,
            reason=f"hedge: guaranteed=${guaranteed_total:.3f} executed={buy_success}",
        )
        await self._send_hedge_alert(
            pos, result_instruction, executed=buy_success, shadow=False
        )
        return buy_success

    async def _record_hedge_decision(
        self,
        pos: MonitoredPosition,
        instruction: dict,
        executed: bool,
        shadow: bool,
    ) -> None:
        """Write HEDGE_EXIT decision to strategy_decisions table."""
        if self._decision_repo is None:
            return
        try:
            import json

            from domain.value_objects import StrategyDecisionRecord

            metadata = {
                "hedge_executed": executed,
                "hedge_shadow": shadow,
                "fill_price": pos.fill_price,
                "fill_size": pos.fill_size,
                "confirmed_size": pos.confirmed_size,
                "opposite_token_id": instruction.get("opposite_token_id"),
                "opposite_ask": instruction.get("opposite_ask"),
                "max_buy_price": instruction.get("max_buy_price"),
                "size_to_buy": instruction.get("size_to_buy"),
                "expected_guaranteed_profit": instruction.get(
                    "expected_guaranteed_profit"
                ),
                "size_matched": instruction.get("size_matched"),
                "order_id": instruction.get("order_id"),
                "consensus_ticks": instruction.get("consensus_ticks"),
                "eval_offset": instruction.get("eval_offset"),
                "held_seconds": time.time() - pos.filled_at_epoch,
            }

            record = StrategyDecisionRecord(
                strategy_id=pos.strategy_id,
                strategy_version="hedge-exit-1.0",
                asset="BTC",
                window_ts=pos.window_ts,
                timeframe="5m",
                eval_offset=instruction.get("eval_offset") or 0,
                mode="SHADOW" if shadow else "LIVE",
                action="HEDGE_EXIT",
                direction=pos.direction,
                confidence=None,
                confidence_score=None,
                entry_cap=None,
                collateral_pct=None,
                entry_reason="",
                skip_reason=None,
                metadata_json=json.dumps(metadata),
                evaluated_at=time.time(),
            )

            await self._decision_repo.write_decision(record)
        except Exception as exc:
            self._log.warning(
                "position_monitor.hedge_record_error",
                error=str(exc)[:200],
            )

    async def _send_hedge_alert(
        self,
        pos: MonitoredPosition,
        instruction: dict,
        executed: bool,
        shadow: bool,
    ) -> None:
        """Send Telegram alert for hedge event."""
        if self._alerter is None:
            return

        held_secs = time.time() - pos.filled_at_epoch
        mode = "SHADOW" if shadow else ("EXECUTED" if executed else "FAILED")
        emoji = {
            "SHADOW": "\U0001f441️",
            "EXECUTED": "\U0001f6e1️",
            "FAILED": "❌",
        }.get(mode, "❓")

        msg = (
            f"{emoji} *HEDGE {mode}* — {pos.strategy_id}\n"
            f"held: `{pos.direction}` window: `{pos.window_ts}`\n"
            f"fill: `${pos.fill_price:.4f}` size: `{pos.fill_size:.2f}`\n"
            f"opposite_ask: `${instruction.get('opposite_ask', 0.0):.4f}` "
            f"max_buy: `${instruction.get('max_buy_price', 0.0):.4f}`\n"
            f"guaranteed: `${instruction.get('expected_guaranteed_profit', 0.0):.3f}` "
            f"held_for: `{held_secs:.0f}s`"
        )

        try:
            send = getattr(self._alerter, "send_raw_message", None)
            if send is not None:
                await send(msg)
            elif hasattr(self._alerter, "send_system_alert"):
                await self._alerter.send_system_alert(msg)
        except Exception as exc:
            self._log.warning(
                "position_monitor.hedge_alert_error",
                error=str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def remove_position(self, strategy_id: str, window_ts: int) -> None:
        """Remove a position from monitoring (e.g. window resolved)."""
        key = f"{strategy_id}:{window_ts}"
        self._positions.pop(key, None)

    def get_open_positions(self) -> dict[str, MonitoredPosition]:
        """Return all currently monitored positions."""
        return dict(self._positions)

    @property
    def position_count(self) -> int:
        return len(self._positions)
