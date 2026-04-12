"""EvaluateStrategiesUseCase -- multi-strategy window evaluation.

Runs ALL registered strategies for a window evaluation, records decisions,
and returns the LIVE strategy's decision for execution.

Feature flag: ENGINE_USE_STRATEGY_PORT (default false).

Audit: SP-04.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Optional

import structlog

from domain.value_objects import (
    EvaluateStrategiesResult,
    StrategyContext,
    StrategyDecision,
    StrategyDecisionRecord,
    StrategyRegistration,
)

log = structlog.get_logger(__name__)

_STRATEGY_TIMEOUT_S = 5.0


class EvaluateStrategiesUseCase:
    """Runs ALL registered strategies for a window evaluation.

    Replaces the single-strategy path in the orchestrator's
    _process_execution_queue.

    Flow:
      1. Build StrategyContext from window + market state + feeds
      2. Run each enabled strategy in parallel (asyncio.gather)
      3. Record ALL decisions (LIVE + GHOST) to StrategyDecisionRepository
      4. Return the LIVE strategy's decision for execution
    """

    def __init__(
        self,
        *,
        strategies: list[tuple[StrategyRegistration, Any]],
        decision_repo: Optional[Any] = None,
        v4_snapshot_port: Optional[Any] = None,
        vpin_calculator: Optional[Any] = None,
        cg_feeds: Optional[dict] = None,
        twap_tracker: Optional[Any] = None,
        db_client: Optional[Any] = None,
        clock: Optional[Any] = None,
    ):
        self._strategies = strategies
        self._decision_repo = decision_repo
        self._v4_port = v4_snapshot_port
        self._vpin = vpin_calculator
        self._cg_feeds = cg_feeds or {}
        self._twap = twap_tracker
        self._db = db_client
        self._clock = clock
        self._write_enabled = os.environ.get(
            "STRATEGY_DECISION_WRITES", "true"
        ).lower() == "true"
        self._v4_enabled = os.environ.get(
            "V4_FUSION_ENABLED", "false"
        ).lower() == "true"

    async def execute(
        self,
        window: Any,
        state: Any,
    ) -> EvaluateStrategiesResult:
        """Evaluate all strategies and return the LIVE decision."""
        asset = getattr(window, "asset", "BTC")
        window_ts = getattr(window, "window_ts", 0)
        eval_offset = getattr(window, "eval_offset", None)
        window_key = f"{asset}-{window_ts}"

        # Build shared context
        ctx = await self._build_context(window, state)

        # Fan out to all enabled strategies
        enabled = [
            (reg, strat) for reg, strat in self._strategies if reg.enabled
        ]

        async def _run_one(
            reg: StrategyRegistration, strat: Any
        ) -> tuple[StrategyRegistration, StrategyDecision]:
            try:
                decision = await asyncio.wait_for(
                    strat.evaluate(ctx), timeout=_STRATEGY_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                decision = StrategyDecision(
                    action="ERROR",
                    direction=None,
                    confidence=None,
                    confidence_score=None,
                    entry_cap=None,
                    collateral_pct=None,
                    strategy_id=reg.strategy_id,
                    strategy_version=getattr(strat, "version", "0.0.0"),
                    entry_reason="",
                    skip_reason=f"timeout after {_STRATEGY_TIMEOUT_S}s",
                )
            except Exception as exc:
                decision = StrategyDecision(
                    action="ERROR",
                    direction=None,
                    confidence=None,
                    confidence_score=None,
                    entry_cap=None,
                    collateral_pct=None,
                    strategy_id=reg.strategy_id,
                    strategy_version=getattr(strat, "version", "0.0.0"),
                    entry_reason="",
                    skip_reason=f"unhandled: {str(exc)[:200]}",
                )
            return (reg, decision)

        results = await asyncio.gather(
            *[_run_one(reg, strat) for reg, strat in enabled],
            return_exceptions=False,
        )

        # Record ALL decisions
        all_decisions: list[StrategyDecision] = []
        live_decision: Optional[StrategyDecision] = None

        now = time.time()
        for reg, decision in results:
            all_decisions.append(decision)

            # Persist
            if self._decision_repo and self._write_enabled:
                try:
                    record = StrategyDecisionRecord(
                        strategy_id=decision.strategy_id,
                        strategy_version=decision.strategy_version,
                        asset=asset,
                        window_ts=window_ts,
                        timeframe=getattr(window, "timeframe", "5m") if hasattr(window, "timeframe") else "5m",
                        eval_offset=eval_offset,
                        mode=reg.mode,
                        action=decision.action,
                        direction=decision.direction,
                        confidence=decision.confidence,
                        confidence_score=decision.confidence_score,
                        entry_cap=decision.entry_cap,
                        collateral_pct=decision.collateral_pct,
                        entry_reason=decision.entry_reason,
                        skip_reason=decision.skip_reason,
                        metadata_json=json.dumps(decision.metadata),
                        evaluated_at=now,
                    )
                    asyncio.create_task(self._safe_write(record))
                except Exception as exc:
                    log.warning(
                        "strategy.record_error",
                        strategy_id=decision.strategy_id,
                        error=str(exc)[:200],
                    )

            # Find LIVE decision
            if reg.mode == "LIVE" and decision.action == "TRADE":
                live_decision = decision

            log.info(
                "strategy.evaluated",
                strategy_id=decision.strategy_id,
                mode=reg.mode,
                action=decision.action,
                direction=decision.direction,
                skip_reason=decision.skip_reason,
            )

        return EvaluateStrategiesResult(
            live_decision=live_decision,
            all_decisions=all_decisions,
            context=ctx,
            window_key=window_key,
            already_traded=False,
        )

    async def _safe_write(self, record: StrategyDecisionRecord) -> None:
        """Write decision record, swallowing errors."""
        try:
            await self._decision_repo.write_decision(record)
        except Exception as exc:
            log.warning("strategy.write_error", error=str(exc)[:200])

    async def _build_context(self, window: Any, state: Any) -> StrategyContext:
        """Build a StrategyContext from window + market state + feeds."""
        asset = getattr(window, "asset", "BTC")
        window_ts = getattr(window, "window_ts", 0)
        eval_offset = getattr(window, "eval_offset", None)
        open_price = getattr(window, "open_price", 0.0) or 0.0

        # Current price
        btc_price = float(getattr(state, "btc_price", 0) or 0)

        # VPIN
        vpin_val = 0.0
        if self._vpin:
            vpin_val = getattr(self._vpin, "current_vpin", 0.0) or 0.0

        # Regime
        regime = "UNKNOWN"
        if self._vpin:
            regime = getattr(self._vpin, "regime", "UNKNOWN") or "UNKNOWN"

        # Deltas -- fetch from DB if available
        delta_chainlink = None
        delta_tiingo = None
        delta_binance = None
        delta_pct = 0.0
        delta_source = "unknown"
        tiingo_close = None

        if self._db:
            try:
                ti_price = await self._db.get_latest_tiingo_price(asset)
                if ti_price and open_price:
                    delta_tiingo = (ti_price - open_price) / open_price
                    tiingo_close = ti_price
            except Exception:
                pass
            try:
                cl_price = await self._db.get_latest_chainlink_price(asset)
                if cl_price and open_price:
                    delta_chainlink = (cl_price - open_price) / open_price
            except Exception:
                pass

        # Binance delta
        if btc_price and open_price:
            delta_binance = (btc_price - open_price) / open_price

        # Select primary delta
        for src, val in [
            ("tiingo_rest_candle", delta_tiingo),
            ("chainlink", delta_chainlink),
            ("binance", delta_binance),
        ]:
            if val is not None:
                delta_pct = val
                delta_source = src
                break

        # TWAP
        twap_delta = None
        if self._twap:
            try:
                twap_result = self._twap.get_result(asset, window_ts)
                if twap_result:
                    twap_delta = getattr(twap_result, "delta_pct", None)
            except Exception:
                pass

        # CoinGlass
        cg_snapshot = None
        cg_feed = self._cg_feeds.get(asset)
        if cg_feed:
            cg_snapshot = getattr(cg_feed, "snapshot", None)

        # CLOB prices
        clob_up_bid = None
        clob_up_ask = None
        clob_down_bid = None
        clob_down_ask = None
        if self._db:
            try:
                clob = await self._db.get_latest_clob_prices(asset)
                if clob:
                    clob_up_bid = clob.get("clob_up_bid")
                    clob_up_ask = clob.get("clob_up_ask")
                    clob_down_bid = clob.get("clob_down_bid")
                    clob_down_ask = clob.get("clob_down_ask")
            except Exception:
                pass

        # V4 snapshot
        v4_snapshot = None
        if self._v4_enabled and self._v4_port:
            try:
                timeframe = "5m"
                if hasattr(window, "duration_secs"):
                    timeframe = "15m" if window.duration_secs == 900 else "5m"
                v4_snapshot = await self._v4_port.get_snapshot(asset, timeframe)
            except Exception as exc:
                log.warning("strategy.v4_fetch_error", error=str(exc)[:200])

        return StrategyContext(
            asset=asset,
            window_ts=window_ts,
            timeframe="5m",
            eval_offset=eval_offset,
            delta_chainlink=delta_chainlink,
            delta_tiingo=delta_tiingo,
            delta_binance=delta_binance,
            delta_pct=delta_pct,
            delta_source=delta_source,
            current_price=btc_price,
            open_price=open_price,
            vpin=vpin_val,
            regime=regime,
            cg_snapshot=cg_snapshot,
            twap_delta=twap_delta,
            tiingo_close=tiingo_close,
            gamma_up_price=getattr(window, "up_price", None),
            gamma_down_price=getattr(window, "down_price", None),
            clob_up_bid=clob_up_bid,
            clob_up_ask=clob_up_ask,
            clob_down_bid=clob_down_bid,
            clob_down_ask=clob_down_ask,
            v4_snapshot=v4_snapshot,
        )
