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
        prior_list = list(prior_decisions)
        traded_earlier = self._collect_prior_trades(prior_list, eval_offset)
        prior_skips = self._collect_prior_skips(prior_list, eval_offset)

        eligible: list[SummaryDecisionLine] = []
        blocked_signal: list[SummaryDecisionLine] = []
        blocked_exec_timing: list[SummaryDecisionLine] = []
        off_window: list[SummaryDecisionLine] = []
        already_traded: list[SummaryDecisionLine] = []
        window_expired: list[SummaryDecisionLine] = []
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
            bounds = self._timing_bounds(cfg)
            window_closed = (
                bounds is not None and eval_offset < bounds[0]
            )

            if earlier_off is not None:
                already_traded.append(
                    SummaryDecisionLine(sid, mode, f"traded at T-{earlier_off}")
                )
            elif (
                self._is_exec_too_late(reason) or self._is_outside_window(reason)
            ) and window_closed:
                # Strategy's eligible window has ended without a trade — the
                # "too late" / "outside window" reason at this offset is
                # circular. Surface the dominant in-window blocker instead.
                bounds_str = f"T-{bounds[1]}..T-{bounds[0]}"
                summary_tail = self._summarize_prior_skips(prior_skips.get(sid, []))
                body = f"window {bounds_str} expired"
                if summary_tail:
                    body += f" — {summary_tail}"
                else:
                    body += " — never evaluated in-window"
                window_expired.append(SummaryDecisionLine(sid, mode, body))
            elif self._is_exec_too_late(reason):
                blocked_exec_timing.append(SummaryDecisionLine(sid, mode, reason))
            elif self._is_outside_window(reason):
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
            window_expired=tuple(window_expired),
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

    @classmethod
    def _collect_prior_skips(
        cls,
        prior_decisions: Iterable[Any],
        current_offset: int,
    ) -> dict[str, list[tuple[int, str]]]:
        """Map strategy_id -> list of (eval_offset, skip_reason) for prior
        SKIP records that fell inside-window relative to now.

        Only SKIPs at offsets > current_offset (earlier in real time, since
        T- countdown) are considered. ERRORs and the trivial final-offset
        "too late / outside window" reasons are filtered — we want the
        in-window blockers that actually explain why the strategy sat on
        its hands.
        """
        by_sid: dict[str, list[tuple[int, str]]] = {}
        for rec in prior_decisions:
            if getattr(rec, "action", None) != "SKIP":
                continue
            off = getattr(rec, "eval_offset", None)
            if off is None or off <= current_offset:
                continue
            reason = getattr(rec, "skip_reason", None) or ""
            if not reason:
                continue
            # Skip the circular reasons that inspired this fix — those
            # don't explain why the strategy never fired.
            if cls._is_exec_too_late(reason) or cls._is_outside_window(reason):
                continue
            by_sid.setdefault(rec.strategy_id, []).append((int(off), reason))
        return by_sid

    @classmethod
    def _summarize_prior_skips(
        cls,
        skips: list[tuple[int, str]],
    ) -> str:
        """Render dominant in-window skip reason as a single tail string.

        Returns '' if no usable in-window skips exist. Otherwise groups by
        normalized category (text before first ':'), picks the most common,
        and annotates with count + representative offsets.

        Example output: ``confidence x3 (T-180, T-150, T-120)``
        """
        if not skips:
            return ""
        # Group by category prefix.
        groups: dict[str, list[tuple[int, str]]] = {}
        for off, reason in skips:
            cat = reason.split(":", 1)[0].strip() or reason[:24]
            groups.setdefault(cat, []).append((off, reason))
        # Dominant category = most occurrences, ties broken by earliest offset.
        cat, items = max(
            groups.items(),
            key=lambda kv: (len(kv[1]), max(o for o, _ in kv[1])),
        )
        items_sorted = sorted(items, key=lambda p: -p[0])  # T-180 before T-90
        offsets = ", ".join(f"T-{o}" for o, _ in items_sorted[:4])
        suffix = "" if len(items_sorted) <= 4 else ", …"
        count = len(items_sorted)
        # If only one distinct category across all skips, lead with a sample
        # reason excerpt so the user sees the concrete blocker text.
        sample = items_sorted[0][1]
        sample_trim = sample if len(sample) <= 60 else sample[:57] + "…"
        if len(groups) == 1:
            return f"{sample_trim} ×{count} ({offsets}{suffix})"
        return f"{cat} ×{count} ({offsets}{suffix})"

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
