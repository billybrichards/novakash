"""Use case: Build Window Summary.

Replaces the inline grouping block in ``StrategyRegistry._send_window_summary``
(registry.py, pre-PR-C L670-775). Pure transformation:

    (decisions, configs, surface, prior_decisions)  →  WindowSummaryContext

The caller (registry) is responsible for:

    - Querying prior strategy_decisions for the same window (to feed
      ``prior_decisions`` — used for the "already traded this window"
      contradiction-killer).
    - Rendering the returned VO to Telegram text via the formatter
      adapter (see ``engine/adapters/alert/window_summary_formatter.py``).

Keeping this use case pure (no I/O, no alerter, no Haiku) makes it
trivially unit-testable — see ``engine/tests/unit/use_cases/``.

PR: C (clean-arch extraction).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from domain.value_objects import (
    StrategyDecision,
    SummaryDecisionLine,
    WindowSummaryContext,
)


class BuildWindowSummaryUseCase:
    """Pure producer of :class:`WindowSummaryContext`.

    No dependencies — deliberately stateless. Instantiate once at
    registry startup and reuse.
    """

    def execute(
        self,
        *,
        window_ts: int,
        eval_offset: int,
        timescale: str,
        open_price: Optional[float],
        current_price: Optional[float],
        sources_agree: str,
        decisions: list[StrategyDecision],
        configs: dict[str, Any],
        prior_decisions: Iterable[Any] = (),
    ) -> WindowSummaryContext:
        """Group decisions into the six buckets.

        Parameters
        ----------
        decisions
            Current-offset decisions (TRADE / SKIP / ERROR).
        configs
            Map of ``strategy_id -> StrategyConfig``. Used for mode
            (LIVE / GHOST) and timing-gate bounds extraction.
        prior_decisions
            Every ``StrategyDecisionRecord`` for this window_ts
            persisted at an earlier eval offset. Iterated once — any
            iterable is fine.
        """
        traded_earlier = self._collect_prior_trades(prior_decisions, eval_offset)

        eligible: list[SummaryDecisionLine] = []
        blocked_signal: list[SummaryDecisionLine] = []
        blocked_exec_timing: list[SummaryDecisionLine] = []
        off_window: list[SummaryDecisionLine] = []
        already_traded: list[SummaryDecisionLine] = []
        ghost_shadow: list[SummaryDecisionLine] = []

        for d in decisions:
            sid = d.strategy_id
            cfg = configs.get(sid)
            mode = getattr(cfg, "mode", "?") if cfg else "?"

            # GHOST: collapse to one bucket regardless of outcome.
            if mode == "GHOST":
                tag = ""
                if d.action == "TRADE":
                    tag = f" (ghost-TRADE {d.direction})"
                ghost_shadow.append(SummaryDecisionLine(sid, mode, f"{sid}{tag}"))
                continue

            # LIVE:
            earlier_off = traded_earlier.get(sid)
            if d.action == "TRADE":
                body = f"TRADE {d.direction}"
                if d.confidence:
                    body += f" | conf={d.confidence}"
                eligible.append(SummaryDecisionLine(sid, mode, body))
                continue

            if d.action != "SKIP":
                blocked_signal.append(SummaryDecisionLine(sid, mode, "ERROR"))
                continue

            reason = d.skip_reason or "unknown"

            if earlier_off is not None:
                already_traded.append(
                    SummaryDecisionLine(sid, mode, f"traded at T-{earlier_off}")
                )
            elif self._is_exec_too_late(reason):
                blocked_exec_timing.append(SummaryDecisionLine(sid, mode, reason))
            elif self._is_outside_window(reason):
                bounds = self._timing_bounds(cfg)
                suffix = f" [T-{bounds[0]}..T-{bounds[1]}]" if bounds else ""
                off_window.append(
                    SummaryDecisionLine(sid, mode, f"outside window{suffix}")
                )
            else:
                blocked_signal.append(SummaryDecisionLine(sid, mode, reason))

        return WindowSummaryContext(
            window_ts=window_ts,
            eval_offset=eval_offset,
            timescale=timescale,
            open_price=open_price,
            current_price=current_price,
            eligible=tuple(eligible),
            blocked_signal=tuple(blocked_signal),
            blocked_exec_timing=tuple(blocked_exec_timing),
            off_window=tuple(off_window),
            already_traded=tuple(already_traded),
            ghost_shadow=tuple(ghost_shadow),
            sources_agree=sources_agree,
        )

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _collect_prior_trades(
        prior_decisions: Iterable[Any],
        current_offset: int,
    ) -> dict[str, int]:
        """Map strategy_id -> earliest-in-time prior TRADE offset.

        Because eval_offset counts DOWN (T-300 is earlier than T-62),
        "earliest trade in the window" corresponds to the largest
        offset value that is still greater than ``current_offset``.
        """
        by_sid: dict[str, int] = {}
        for rec in prior_decisions:
            if getattr(rec, "action", None) != "TRADE":
                continue
            off = getattr(rec, "eval_offset", None)
            if off is None or off <= current_offset:
                continue
            existing = by_sid.get(rec.strategy_id)
            if existing is None or off > existing:
                by_sid[rec.strategy_id] = off
        return by_sid

    @staticmethod
    def _is_exec_too_late(reason: str) -> bool:
        """Execution-safety 'too late' (custom hook), NOT YAML outside-window."""
        return "too late" in reason and "outside window" not in reason

    @staticmethod
    def _is_outside_window(reason: str) -> bool:
        """YAML timing gate or custom 'outside window' hook."""
        if reason.startswith("timing:") and " outside [" in reason:
            return True
        if "outside window" in reason:
            return True
        return False

    @staticmethod
    def _timing_bounds(cfg_obj: Any) -> Optional[tuple[int, int]]:
        if not cfg_obj or not getattr(cfg_obj, "gates", None):
            return None
        for g in cfg_obj.gates:
            gtype = g.get("type") if isinstance(g, dict) else getattr(g, "type", None)
            if gtype != "timing":
                continue
            params = (
                g.get("params", {})
                if isinstance(g, dict)
                else (getattr(g, "params", {}) or {})
            )
            lo = params.get("min_offset")
            hi = params.get("max_offset")
            if lo is not None and hi is not None:
                return (int(lo), int(hi))
        return None
