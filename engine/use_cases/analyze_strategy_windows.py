"""Use case: analyze one strategy across many window evaluation ticks."""

from __future__ import annotations

import json

from domain.value_objects import StrategyWindowAnalysis


class AnalyzeStrategyWindowsUseCase:
    """Group a strategy's evaluations into tradeable vs non-tradeable buckets."""

    def __init__(self, *, trace_repo, decision_repo) -> None:
        self._trace_repo = trace_repo
        self._decision_repo = decision_repo

    async def execute(
        self,
        *,
        asset: str,
        timeframe: str,
        strategy_id: str,
        start_window_ts: int,
        end_window_ts: int,
    ) -> StrategyWindowAnalysis:
        decisions = await self._decision_repo.get_decisions_in_range(
            asset=asset,
            timeframe=timeframe,
            strategy_id=strategy_id,
            start_window_ts=start_window_ts,
            end_window_ts=end_window_ts,
        )
        traces = await self._trace_repo.get_window_evaluation_traces_in_range(
            asset=asset,
            timeframe=timeframe,
            start_window_ts=start_window_ts,
            end_window_ts=end_window_ts,
        )
        trace_map = {(t.window_ts, t.eval_offset): t.surface_data for t in traces}

        tradeable_evaluations = 0
        inactive_evaluations = 0
        blocked_by_timing = 0
        blocked_by_signal = 0
        executed_trades = 0
        recent_tradeable_examples: list[dict] = []
        recent_non_tradeable_examples: list[dict] = []
        latest_surface_examples: list[dict] = []

        for decision in decisions:
            key = (decision.window_ts, decision.eval_offset)
            surface = trace_map.get(key, {})
            try:
                metadata = json.loads(decision.metadata_json or "{}")
            except Exception:
                metadata = {}
            example = {
                "window_ts": decision.window_ts,
                "eval_offset": decision.eval_offset,
                "action": decision.action,
                "direction": decision.direction,
                "skip_reason": decision.skip_reason,
                "entry_cap": decision.entry_cap,
                "confidence": decision.confidence,
                "surface": surface,
                "metadata": metadata,
            }

            if len(latest_surface_examples) < 10:
                latest_surface_examples.append(example)

            if decision.executed:
                executed_trades += 1

            if decision.action == "TRADE":
                tradeable_evaluations += 1
                if len(recent_tradeable_examples) < 10:
                    recent_tradeable_examples.append(example)
                continue

            reason = decision.skip_reason or ""
            if reason.startswith("timing:") and " outside [" in reason:
                inactive_evaluations += 1
            elif "too late" in reason or "timing=late" in reason:
                blocked_by_timing += 1
            else:
                blocked_by_signal += 1

            if len(recent_non_tradeable_examples) < 10:
                recent_non_tradeable_examples.append(example)

        total = len(decisions)
        non_tradeable = total - tradeable_evaluations
        return StrategyWindowAnalysis(
            strategy_id=strategy_id,
            timeframe=timeframe,
            asset=asset,
            total_evaluations=total,
            tradeable_evaluations=tradeable_evaluations,
            non_tradeable_evaluations=non_tradeable,
            executed_trades=executed_trades,
            inactive_evaluations=inactive_evaluations,
            blocked_by_timing=blocked_by_timing,
            blocked_by_signal=blocked_by_signal,
            latest_surface_examples=latest_surface_examples,
            recent_tradeable_examples=recent_tradeable_examples,
            recent_non_tradeable_examples=recent_non_tradeable_examples,
        )
