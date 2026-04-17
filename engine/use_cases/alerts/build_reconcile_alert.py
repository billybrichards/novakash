"""Use case: BuildReconcileAlert.

Assembles a ``ReconcilePayload`` for the 5-minute reconciler pass.

Key responsibilities:
  - Dedupe orphan reporting — only emit when the orphan roster CHANGES
    (screenshot evidence: same 11/26 orphans re-emitted every pass today).
  - Detect orphan drift (count grew from 11→26 in 9h) — emit OrphanDrift
    event even when matched trades are empty.
  - Group matched rows by (timeframe, strategy_id) — Phase K.

State held per instance: the set of reported orphan condition_ids. Reset
only when roster transitions 0→N (new orphan class appears) or an
external reset is requested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    CumulativeTally,
    LifecyclePhase,
    MatchedTradeRow,
    OrphanDrift,
    ReconcilePayload,
)
from use_cases.ports import Clock


@dataclass
class BuildReconcileAlertInput:
    """Inputs for a single reconcile pass."""

    matched: list[MatchedTradeRow]
    paper_matched: list[MatchedTradeRow]
    current_orphan_condition_ids: list[str]
    orphan_auto_redeemed_wins: int
    orphan_worthless_tokens: int
    event_ts_unix: int
    cumulative: Optional[CumulativeTally] = None
    window_id: Optional[str] = None


class BuildReconcileAlertUseCase:
    """Stateful builder — remembers which orphans were already reported."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._reported_orphan_ids: set[str] = set()
        self._last_orphan_count: int = 0

    def reset_orphan_state(self) -> None:
        """Clear dedupe state; next emission will report all orphans."""
        self._reported_orphan_ids.clear()
        self._last_orphan_count = 0

    async def execute(
        self, inp: BuildReconcileAlertInput
    ) -> Optional[ReconcilePayload]:
        """Return a payload if there's anything to emit; None if silent-safe.

        Emits if ANY of:
          - matched or paper_matched has at least one row
          - orphan drift (count or new ids) since last emission
        Silent otherwise (no matched + no drift).
        """
        # Determine drift vs last known state.
        current_ids = set(inp.current_orphan_condition_ids)
        new_ids = tuple(sorted(current_ids - self._reported_orphan_ids))
        current_count = len(current_ids)

        drift: Optional[OrphanDrift] = None
        count_changed = current_count != self._last_orphan_count
        if new_ids or count_changed:
            drift = OrphanDrift(
                prior_count=self._last_orphan_count,
                current_count=current_count,
                new_condition_ids=new_ids,
                auto_redeemed_wins=inp.orphan_auto_redeemed_wins,
                worthless_tokens=inp.orphan_worthless_tokens,
            )

        # Update remembered state AFTER computing the drift so next call
        # sees today's roster as the new baseline.
        self._reported_orphan_ids = current_ids
        self._last_orphan_count = current_count

        has_matched = bool(inp.matched) or bool(inp.paper_matched)

        if not has_matched and drift is None:
            return None

        now_unix = int(self._clock.now())
        return ReconcilePayload(
            header=AlertHeader(
                phase=LifecyclePhase.OPS,
                title="Reconcile pass",
                event_ts_unix=inp.event_ts_unix,
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
            ),
            footer=AlertFooter(
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
            ),
            tier=AlertTier.DIAGNOSTIC,
            matched=tuple(inp.matched),
            paper_matched=tuple(inp.paper_matched),
            orphan_drift=drift,
            cumulative=inp.cumulative,
        )
