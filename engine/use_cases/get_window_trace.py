"""Use case: get a grouped window trace view for one evaluation tick."""

from __future__ import annotations

from domain.value_objects import WindowTraceView


class GetWindowTraceUseCase:
    """Build a window-centric view from stored traces and decisions."""

    def __init__(self, *, trace_repo, decision_repo) -> None:
        self._trace_repo = trace_repo
        self._decision_repo = decision_repo

    async def execute(
        self,
        *,
        asset: str,
        window_ts: int,
        timeframe: str,
        eval_offset: int | None = None,
    ) -> WindowTraceView | None:
        window_trace = await self._trace_repo.get_window_evaluation_trace(
            asset=asset,
            window_ts=window_ts,
            timeframe=timeframe,
            eval_offset=eval_offset,
        )
        if window_trace is None:
            return None

        decisions = await self._decision_repo.get_decisions_for_window(asset, window_ts)
        decisions = [d for d in decisions if d.timeframe == timeframe]
        if eval_offset is not None:
            decisions = [d for d in decisions if d.eval_offset == eval_offset]
        gate_checks = await self._trace_repo.get_gate_check_traces(
            asset, window_ts, timeframe
        )
        if eval_offset is not None:
            gate_checks = [g for g in gate_checks if g.eval_offset == eval_offset]

        eligible_now: list[str] = []
        blocked_by_signal: list[str] = []
        blocked_by_timing: list[str] = []
        inactive_this_offset: list[str] = []

        for decision in decisions:
            label = f"{decision.strategy_id} ({decision.mode})"
            reason = decision.skip_reason or ""
            if decision.action == "TRADE":
                direction = decision.direction or "?"
                conf = f" | conf={decision.confidence}" if decision.confidence else ""
                eligible_now.append(f"{label}: TRADE {direction}{conf}")
            elif decision.action == "SKIP":
                if reason.startswith("timing:") and " outside [" in reason:
                    inactive_this_offset.append(label)
                elif "too late" in reason or "timing=late" in reason:
                    blocked_by_timing.append(f"{label}: {reason}")
                else:
                    blocked_by_signal.append(f"{label}: {reason}")
            else:
                blocked_by_signal.append(f"{label}: ERROR")

        return WindowTraceView(
            asset=window_trace.asset,
            window_ts=window_trace.window_ts,
            timeframe=window_trace.timeframe,
            eval_offset=window_trace.eval_offset,
            surface_data=window_trace.surface_data,
            strategy_decisions=decisions,
            gate_checks=gate_checks,
            eligible_now=eligible_now,
            blocked_by_signal=blocked_by_signal,
            blocked_by_timing=blocked_by_timing,
            inactive_this_offset=inactive_this_offset,
        )
