"""Exit monitor wire-up module — integrates shadow detection into the engine.

Called from two hook points:

1. Per-tick hook (registry.evaluate_all tail): ``run_shadow_monitor()``
   Evaluates open trades against TickFormer p_against thresholds. Fire-and-
   forget via asyncio.create_task. Any exception is caught and logged at
   WARNING — NEVER crashes the engine.

2. Window-resolution hook: ``run_backfill()``
   Called wherever the engine stamps WIN/LOSS/PUSH outcomes. Updates
   realized_outcome + pnl fields on existing shadow rows.

Feature flag: EXIT_MONITOR_SHADOW_ENABLED env var (default 'false').

Usage (in registry.py, after the position_monitor exit-eval block):

    from exit_monitor.wireup import get_shadow_monitor, run_shadow_monitor
    _mon = get_shadow_monitor(db_client=self._db)
    if _mon is not None:
        import asyncio
        asyncio.create_task(
            run_shadow_monitor(
                monitor=_mon,
                surface=surface,
                position_monitor=self._position_monitor,
            )
        )

Usage (in window-resolution path):

    from exit_monitor.wireup import run_backfill
    asyncio.create_task(
        run_backfill(
            db_client=self._db,
            decision_id=decision_id,
            outcome='WIN',
            fill_price=fill_price,
        )
    )
"""
from __future__ import annotations

import os
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

# Module-level singleton — one monitor instance per engine process.
_MONITOR: Optional[Any] = None
_ENABLED: Optional[bool] = None


def is_enabled() -> bool:
    """Return True when EXIT_MONITOR_SHADOW_ENABLED=true."""
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = (
            os.environ.get("EXIT_MONITOR_SHADOW_ENABLED", "false").lower() == "true"
        )
    return _ENABLED


def get_shadow_monitor(db_client: Any = None, db_pool: Any = None) -> Optional[Any]:
    """Return the module-level MonitorOpenTradesUseCase singleton.

    Returns None when EXIT_MONITOR_SHADOW_ENABLED is false or init fails.
    Safe to call every tick — singleton is built once.
    """
    if not is_enabled():
        return None

    global _MONITOR
    if _MONITOR is not None:
        return _MONITOR

    try:
        from exit_monitor.adapters.shadow_exit_gateway import ShadowExitGateway
        from exit_monitor.use_cases.monitor_open_trades import (
            MonitorOpenTradesUseCase,
        )

        gateway = ShadowExitGateway(db_pool=db_pool, db_client=db_client)
        _MONITOR = MonitorOpenTradesUseCase(repo=gateway)
        log.info("exit_monitor.shadow_monitor_initialized")
    except Exception as exc:
        log.warning(
            "exit_monitor.shadow_monitor_init_error", error=str(exc)[:200]
        )
        _MONITOR = None

    return _MONITOR


async def run_shadow_monitor(
    *,
    monitor: Any,
    surface: Any,
    position_monitor: Any,
    eval_offset: Optional[int] = None,
) -> None:
    """Evaluate open trades against TickFormer thresholds on one tick.

    Pulls open trade state from position_monitor.get_open_positions()
    and builds the inputs needed by MonitorOpenTradesUseCase.execute().

    All exceptions are swallowed — this MUST NOT crash the engine.
    """
    if monitor is None:
        return
    try:
        from exit_monitor.adapters.clob_snapshot_reader import read_clob_snapshot
        from exit_monitor.adapters.tickformer_prob_reader import (
            detect_tickformer_model,
            read_tickformer_prob,
        )
        from exit_monitor.use_cases.monitor_open_trades import (
            CLOBSnapshot,
            OpenTradeState,
        )

        if position_monitor is None:
            return

        positions = position_monitor.get_open_positions()
        if not positions:
            return

        # Resolve eval_offset from surface if not supplied
        if eval_offset is None:
            eval_offset = int(getattr(surface, "eval_offset", 0) or 0)

        # Build per-trade open_trade_state list
        open_trades: list[OpenTradeState] = []
        for pos_key, pos in positions.items():
            # decision_id: use order_id as proxy when strategy_decisions.id
            # is not directly on the MonitoredPosition. The existing
            # PositionMonitor does not store decision_id from the DB row.
            # We use a stable hash of (strategy_id, window_ts) as integer
            # surrogate — consistent within a window lifetime, never a real FK.
            # FUTURE: if strategy_decisions.id is threaded through on_fill,
            # use it directly here.
            import hashlib
            _did_raw = f"{pos.strategy_id}:{pos.window_ts}".encode()
            decision_id = int(hashlib.md5(_did_raw).hexdigest()[:12], 16)

            open_trades.append(
                OpenTradeState(
                    decision_id=decision_id,
                    asset=getattr(pos, "strategy_id", "").split(":")[0]
                    if ":" in getattr(pos, "strategy_id", "")
                    else _infer_asset_from_surface(surface),
                    window_ts=pos.window_ts,
                    strategy_id=pos.strategy_id,
                    side=pos.direction,
                    entry_p=None,
                    entry_eval_offset=None,
                )
            )

        # Build surface lookup (single surface = single asset per tick)
        asset = _infer_asset_from_surface(surface)
        surface_by_asset = {t.asset: surface for t in open_trades}

        # Prob lookup
        prob_by_asset: dict[str, float] = {}
        for trade in open_trades:
            model = detect_tickformer_model(trade.strategy_id)
            prob = read_tickformer_prob(surface_by_asset.get(trade.asset), model)
            if prob is not None:
                prob_by_asset[trade.asset] = prob

        # CLOB snapshots — one per asset using the first trade's side
        seen_assets: set[str] = set()
        clob_by_asset: dict[str, CLOBSnapshot] = {}
        for trade in open_trades:
            if trade.asset not in seen_assets:
                seen_assets.add(trade.asset)
                clob_by_asset[trade.asset] = read_clob_snapshot(
                    surface_by_asset.get(trade.asset), trade.side
                )

        eval_offset_by_asset = {t.asset: eval_offset for t in open_trades}

        await monitor.execute(
            open_trades=open_trades,
            prob_tickformer_by_asset=prob_by_asset,
            clob_by_asset=clob_by_asset,
            eval_offset_by_asset=eval_offset_by_asset,
        )
    except Exception as exc:
        log.warning(
            "exit_monitor.run_shadow_monitor_error", error=str(exc)[:300]
        )


async def run_backfill(
    *,
    db_client: Any,
    decision_id: int,
    outcome: str,
    fill_price: float,
    db_pool: Any = None,
) -> None:
    """Backfill realized outcome onto shadow rows for a resolved decision.

    Wraps BackfillRealizedOutcomeUseCase. Swallows all exceptions.
    """
    if not is_enabled():
        return
    try:
        from exit_monitor.adapters.shadow_exit_gateway import ShadowExitGateway
        from exit_monitor.use_cases.backfill_realized_outcome import (
            BackfillRealizedOutcomeUseCase,
        )

        gateway = ShadowExitGateway(db_pool=db_pool, db_client=db_client)
        uc = BackfillRealizedOutcomeUseCase(repo=gateway)
        await uc.execute(
            decision_id=decision_id,
            outcome=outcome,  # type: ignore[arg-type]
            fill_price=fill_price,
        )
    except Exception as exc:
        log.warning(
            "exit_monitor.run_backfill_error",
            decision_id=decision_id,
            error=str(exc)[:200],
        )


def _infer_asset_from_surface(surface: Any) -> str:
    """Extract asset name from the surface, defaulting to 'BTC'."""
    for attr in ("asset", "symbol", "market_asset"):
        v = getattr(surface, attr, None)
        if v and isinstance(v, str):
            return v.upper()
    return "BTC"
