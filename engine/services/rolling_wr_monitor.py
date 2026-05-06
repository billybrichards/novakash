"""RollingWRMonitor — auto-pause cells crossing drawdown triggers.

Audits #379 + #385 (2026-05-06). Hub note #350 + #348 hold the alpha-cell
analysis that motivates this service.

Design:
* `on_trade_resolved(trade)` is called by the order manager / reconciler
  after every WIN/LOSS resolution. The trade dict carries enough metadata
  (strategy_id, direction, eval_offset, hour_utc, regime, fill_price, pnl)
  to bucket into a (strategy, direction, t_band, regime, session) cell.
* For the bucketed cell, compute 60-min rolling: trade count, wins,
  Wilson-LB win rate, total PnL.
* Pause triggers (any one fires):
    - Wilson LB < fill-adjusted breakeven (e.g. fill 0.74 => need 74% WR)
    - WR drop > 25pp from the 7d cell baseline (when baseline is known)
    - 60m PnL < `min_pnl_window_usd` (default -$30)
* On pause: INSERT into `cell_pauses` with `pause_until = NOW() + 4h` and
  fire a Telegram alert (when `alerter` was passed at construction).
* `release(pause_id, by="ops")` flips `released_at = NOW()` so a single
  pause can be cleared manually.

The DB layer is abstracted via a tiny `CellPauseRepo` Protocol so the
monitor can be unit-tested with an in-memory fake.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from services.cell_bucketing import session as _session
from services.cell_bucketing import t_band as _t_band

log = logging.getLogger(__name__)

# Auto-pause duration (sec) when a trigger fires. 4 hours = enough to
# bridge a regime shift; ops can manually release earlier via release().
_DEFAULT_PAUSE_SECONDS = 4 * 60 * 60

# Lookback for rolling-WR window.
_DEFAULT_LOOKBACK_SECONDS = 60 * 60

# Minimum trades before a trigger can fire — small samples generate too
# many false positives.
_DEFAULT_MIN_TRADES = 4

# Default 60-min PnL floor (USD). Crossing this fires a pause regardless
# of WR / Wilson-LB.
_DEFAULT_MIN_PNL_WINDOW_USD = -30.0

# Default WR drop (percentage points) versus the 7d baseline that fires
# a pause. 25pp is the value Hub note #350 cites.
_DEFAULT_MAX_WR_DROP_PP = 25.0


@dataclass(frozen=True)
class CellKey:
    """Tuple identifying a (strategy, direction, t_band, regime, session)
    cell for the rolling-WR monitor."""

    strategy_id: str
    direction: str  # "UP" | "DOWN"
    t_band: str
    regime: Optional[str]
    session: Optional[str]


@dataclass
class ResolvedTrade:
    """Lightweight value object for a resolved trade. Mirrors the subset of
    columns needed by the rolling-WR monitor — order managers can pass
    raw rows or wrap them in this dataclass for clarity."""

    strategy_id: str
    direction: str
    eval_offset: Optional[int]
    hour_utc: Optional[int]
    regime: Optional[str]
    fill_price: Optional[float]
    pnl_usd: float
    is_win: bool
    resolved_at: float = field(default_factory=time.time)

    @property
    def cell(self) -> CellKey:
        return CellKey(
            strategy_id=self.strategy_id,
            direction=self.direction,
            t_band=_t_band(self.eval_offset),
            regime=self.regime,
            session=_session(self.hour_utc),
        )


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson lower bound on the win rate at 95% confidence.

    Returns 0.0 for n=0 (no data). Used by the rolling-WR monitor to avoid
    triggering pauses on small samples whose point estimate happens to be
    low — the LB shrinks toward 0 as n shrinks.
    """
    if n <= 0:
        return 0.0
    p_hat = wins / n
    denom = 1 + z * z / n
    centre = p_hat + z * z / (2 * n)
    margin = z * math.sqrt(
        (p_hat * (1 - p_hat) + z * z / (4 * n)) / n
    )
    return max(0.0, (centre - margin) / denom)


def fill_breakeven_wr(fill_price: float, fee_mult: float = 0.072) -> float:
    """Win rate required to net positive at the given fill price.

    For a buy at `fill_price`, profit per win = (1 - fill) - fee*fill,
    loss per loss = fill (full stake at fill price). Breakeven WR solves:
        WR * (1 - fill - fee*fill) = (1 - WR) * fill
        WR = fill / (1 - fee*fill)
    """
    if fill_price <= 0.0 or fill_price >= 1.0:
        return 1.0
    denom = 1.0 - fee_mult * fill_price
    if denom <= 0:
        return 1.0
    return min(1.0, fill_price / denom)


class CellPauseRepo(Protocol):
    """Persistence boundary for the rolling-WR monitor.

    Implementations:
        * `engine/persistence/db_client.py`: thin asyncpg wrappers
        * `engine/tests/services/test_rolling_wr_monitor.py`: in-memory fake
    """

    async def get_recent_trades_for_cell(
        self, cell: CellKey, lookback_seconds: int
    ) -> list[ResolvedTrade]:
        """Return trades resolved in the last `lookback_seconds` for cell."""
        ...

    async def get_baseline_wr(
        self, cell: CellKey, lookback_seconds: int
    ) -> Optional[float]:
        """Return baseline WR over `lookback_seconds` (e.g. 7 days). None
        when there isn't enough history to compute a stable baseline."""
        ...

    async def is_cell_paused(self, cell: CellKey) -> bool:
        """Quick gate-side lookup: any active pause for this cell?"""
        ...

    async def insert_pause(
        self,
        cell: CellKey,
        pause_seconds: int,
        reason: str,
        trigger_metric: dict[str, Any],
    ) -> Optional[int]:
        """Insert a row into cell_pauses with `pause_until = NOW() +
        pause_seconds`. Returns the new row id, or None if the cell is
        already actively paused (UNIQUE collision is fine — the monitor
        treats it as "pause already in flight")."""
        ...

    async def release_pause(
        self, pause_id: int, released_by: str
    ) -> None:
        """Mark `released_at = NOW()`, `released_by = released_by`."""
        ...


class RollingWRMonitor:
    """Watches resolved trades and pauses underperforming cells."""

    def __init__(
        self,
        repo: CellPauseRepo,
        alerter: Any = None,
        *,
        lookback_seconds: int = _DEFAULT_LOOKBACK_SECONDS,
        pause_seconds: int = _DEFAULT_PAUSE_SECONDS,
        min_trades: int = _DEFAULT_MIN_TRADES,
        min_pnl_window_usd: float = _DEFAULT_MIN_PNL_WINDOW_USD,
        max_wr_drop_pp: float = _DEFAULT_MAX_WR_DROP_PP,
        baseline_lookback_seconds: int = 7 * 24 * 60 * 60,
        fee_mult: float = 0.072,
    ) -> None:
        self._repo = repo
        self._alerter = alerter
        self._lookback = int(lookback_seconds)
        self._pause_seconds = int(pause_seconds)
        self._min_trades = int(min_trades)
        self._min_pnl_window_usd = float(min_pnl_window_usd)
        self._max_wr_drop_pp = float(max_wr_drop_pp)
        self._baseline_lookback = int(baseline_lookback_seconds)
        self._fee_mult = float(fee_mult)

    async def on_trade_resolved(self, trade: ResolvedTrade) -> Optional[int]:
        """Callback for every WIN/LOSS resolution. Returns the inserted
        pause id when a trigger fired, otherwise None."""
        cell = trade.cell

        if await self._repo.is_cell_paused(cell):
            # Already paused — don't re-trigger, don't extend.
            return None

        recent = await self._repo.get_recent_trades_for_cell(
            cell, self._lookback
        )
        if len(recent) < self._min_trades:
            return None

        wins = sum(1 for t in recent if t.is_win)
        n = len(recent)
        wr = wins / n
        wilson_lb = wilson_lower_bound(wins, n)
        pnl_window = sum(t.pnl_usd for t in recent)

        # Trigger 1: Wilson LB below fill-adjusted breakeven.
        # Use the cell's average fill (more representative than the latest
        # trade's fill alone).
        fills = [t.fill_price for t in recent if t.fill_price is not None]
        avg_fill = sum(fills) / len(fills) if fills else 0.5
        breakeven = fill_breakeven_wr(avg_fill, self._fee_mult)
        wilson_below_breakeven = wilson_lb < breakeven

        # Trigger 2: WR drop > N pp from baseline.
        baseline = await self._repo.get_baseline_wr(
            cell, self._baseline_lookback
        )
        wr_drop_breach = (
            baseline is not None
            and (baseline - wr) * 100 > self._max_wr_drop_pp
        )

        # Trigger 3: 60m PnL collapse.
        pnl_breach = pnl_window < self._min_pnl_window_usd

        if not (wilson_below_breakeven or wr_drop_breach or pnl_breach):
            return None

        reasons = []
        if wilson_below_breakeven:
            reasons.append(
                f"wilson_lb={wilson_lb:.3f} < breakeven={breakeven:.3f}"
            )
        if wr_drop_breach:
            reasons.append(
                f"wr_drop={(baseline - wr) * 100:.1f}pp > "
                f"{self._max_wr_drop_pp:.1f}pp (baseline={baseline:.2%})"
            )
        if pnl_breach:
            reasons.append(
                f"60m_pnl=${pnl_window:.2f} < ${self._min_pnl_window_usd:.2f}"
            )
        reason = "; ".join(reasons)

        trigger_metric = {
            "trades": n,
            "wins": wins,
            "wr": round(wr, 4),
            "wilson_lb": round(wilson_lb, 4),
            "avg_fill": round(avg_fill, 4),
            "breakeven_wr": round(breakeven, 4),
            "pnl_window_usd": round(pnl_window, 2),
            "baseline_wr": round(baseline, 4) if baseline is not None else None,
            "lookback_seconds": self._lookback,
        }

        pause_id = await self._repo.insert_pause(
            cell,
            self._pause_seconds,
            reason,
            trigger_metric,
        )

        if pause_id is not None:
            await self._alert(cell, reason, trigger_metric)
            log.warning(
                "rolling_wr_monitor.cell_paused",
                extra={
                    "strategy_id": cell.strategy_id,
                    "direction": cell.direction,
                    "t_band": cell.t_band,
                    "regime": cell.regime,
                    "session": cell.session,
                    "reason": reason,
                    **trigger_metric,
                },
            )

        return pause_id

    async def release(self, pause_id: int, by: str = "ops") -> None:
        """Manually release a pause."""
        await self._repo.release_pause(pause_id, by)

    async def _alert(
        self,
        cell: CellKey,
        reason: str,
        trigger_metric: dict[str, Any],
    ) -> None:
        if self._alerter is None:
            return
        until = datetime.now(timezone.utc) + timedelta(
            seconds=self._pause_seconds
        )
        msg = (
            "[CELL PAUSED]\n"
            f"strategy: {cell.strategy_id}\n"
            f"cell: {cell.direction} / {cell.t_band} / "
            f"{cell.regime or '*'} / {cell.session or '*'}\n"
            f"reason: {reason}\n"
            f"resumes: {until.isoformat()}"
        )
        try:
            send = getattr(self._alerter, "_send", None) or getattr(
                self._alerter, "send", None
            )
            if send is None:
                return
            res = send(msg)
            if hasattr(res, "__await__"):
                await res
        except Exception as exc:  # pragma: no cover — alerter best-effort
            log.warning(
                "rolling_wr_monitor.alert_failed", extra={"error": str(exc)}
            )
