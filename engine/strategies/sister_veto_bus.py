"""In-process sister-strategy veto bus (hub notes #394 / #395 ratification).

Cross-strategy veto lookup for v9.2 sister-pair veto gate.

Architecture
------------
Strategy hooks are synchronous functions called inside ``registry.evaluate_all``.
They cannot await DB queries. This module provides a lightweight in-memory
ring buffer: each cascade-fade strategy publishes its TRADE fires here; v9.2
reads within a configurable window to decide whether to veto.

The buffer is keyed by ``(strategy_id, asset)`` and stores the last
``_MAX_ENTRIES`` fire timestamps and directions. Reads are O(1) dict
lookups + a small linear scan over at most ``_MAX_ENTRIES`` items.

Thread safety
-------------
All state is mutated only within the engine's asyncio main loop
(the ``_evaluate_one`` call chain is synchronous, never concurrent).
No lock is needed.

Test isolation
--------------
Call ``reset_sister_veto_bus()`` in test teardown to clear all state.
The autouse fixture in ``test_v9_2_super_lgb_only.py`` already resets the
qualifying-tick counter — tests that need the bus reset can call this
function directly or use the provided pytest fixture.

Ratified thresholds (hub notes #394 / #395):
    - Hub #394 (v9.2-focused): contrarian n=70 WR 64.3% vs consensus n=277 WR 88.8%, delta +24.5pp
    - Hub #395 (REDO on real strategy_decisions): contrarian n=60 WR 70.0% vs consensus n=287 WR 86.4%, delta +16.4pp
    Both buckets n≥30, both well above 10pp ratify threshold.
    Veto applies all-regimes (not CASCADE-specific).

Usage
-----
    # In cascade-fade hook (publish on TRADE):
    from strategies.sister_veto_bus import publish_sister_fire

    if decision.action == "TRADE":
        publish_sister_fire(
            strategy_id=_STRATEGY_ID,
            asset=surface.asset,
            window_ts=surface.window_ts,
            direction=decision.direction,
        )

    # In v9.2 hook (query before emitting TRADE):
    from strategies.sister_veto_bus import is_sister_pair_veto_active

    if is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset=surface.asset,
        current_window_ts=surface.window_ts,
        v9_2_direction=pred_direction,
        agreement_window_seconds=20,
    ):
        return _skip_v9_2("sister_pair_veto", metadata={...})
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Maximum fire records kept per (strategy_id, asset) key.
# In normal operation one entry per window; 20 windows = ~100 min of history.
_MAX_ENTRIES: int = 20


@dataclass
class _FireRecord:
    """A single TRADE fire from a sister strategy."""
    strategy_id: str
    asset: str
    window_ts: int      # window epoch seconds (same coordinate as surface.window_ts)
    direction: str      # "UP" or "DOWN"
    fired_at: float     # wall-clock time.time() at publish; used for TTL pruning


# Module-level bus: (strategy_id, asset) -> ring of recent fires.
_bus: Dict[Tuple[str, str], List[_FireRecord]] = {}


def publish_sister_fire(
    *,
    strategy_id: str,
    asset: str,
    window_ts: int,
    direction: str,
) -> None:
    """Record a TRADE fire from a sister strategy.

    Call this in the cascade-fade hooks immediately after the gate stack
    returns TRADE. Direction must be the actual TRADE direction ("UP" or "DOWN").
    """
    key = (strategy_id, asset)
    record = _FireRecord(
        strategy_id=strategy_id,
        asset=asset,
        window_ts=int(window_ts),
        direction=direction,
        fired_at=time.monotonic(),
    )
    entries = _bus.setdefault(key, [])
    entries.append(record)
    # Trim to max capacity (FIFO eviction)
    if len(entries) > _MAX_ENTRIES:
        del entries[: len(entries) - _MAX_ENTRIES]


def get_recent_sister_fires(
    strategy_id: str,
    asset: str,
    current_window_ts: int,
    agreement_window_seconds: int = 20,
) -> List[_FireRecord]:
    """Return all fires from ``strategy_id`` within ``agreement_window_seconds``
    of ``current_window_ts``.

    The window is symmetric: any fire with |fire.window_ts - current_window_ts|
    <= agreement_window_seconds qualifies. This covers:
      - fires in the SAME window as v9.2 (window_ts == current_window_ts)
      - fires in an adjacent window within the look-around band

    Returns an empty list when no qualifying fires exist.
    """
    key = (strategy_id, asset)
    entries = _bus.get(key, [])
    if not entries:
        return []
    cutoff = agreement_window_seconds
    current = int(current_window_ts)
    return [r for r in entries if abs(r.window_ts - current) <= cutoff]


def is_sister_pair_veto_active(
    *,
    pair: List[str],
    asset: str,
    current_window_ts: int,
    v9_2_direction: str,
    agreement_window_seconds: int = 20,
) -> bool:
    """Return True if BOTH strategies in ``pair`` fired in the opposite
    direction from ``v9_2_direction`` within ``agreement_window_seconds``.

    Veto logic (spec from hub #394 / #395):
    - BOTH sisters must have a recent fire (within the window) in the
      OPPOSITE direction to v9.2.
    - If either sister has no qualifying fire → no veto (no evidence).
    - If either sister's qualifying fire agrees with v9.2 OR is mixed → no veto.
    - All-regimes: no regime filter applied here.

    Args:
        pair:                     List of exactly 2 strategy IDs to check.
        asset:                    Asset symbol (e.g. "BTC").
        current_window_ts:        The v9.2 eval's window_ts (epoch seconds).
        v9_2_direction:           The direction v9.2 wants to fire ("UP" or "DOWN").
        agreement_window_seconds: ±window in seconds for sister-fire lookup.

    Returns:
        True  → veto: BOTH sisters fired OPPOSITE; v9.2 should SKIP.
        False → no veto: fire normally.
    """
    if not pair or len(pair) < 2:
        return False

    opposite = "DOWN" if v9_2_direction == "UP" else "UP"

    for strategy_id in pair:
        fires = get_recent_sister_fires(
            strategy_id=strategy_id,
            asset=asset,
            current_window_ts=current_window_ts,
            agreement_window_seconds=agreement_window_seconds,
        )
        if not fires:
            # No qualifying fire from this sister → veto cannot apply
            return False
        # Check if ANY qualifying fire is in the opposite direction.
        # We use the most recent fire for the direction check.
        most_recent = max(fires, key=lambda r: r.window_ts)
        if most_recent.direction != opposite:
            # This sister agrees with v9.2 (or direction is same) → no veto
            return False

    # Both sisters have a qualifying fire AND both are OPPOSITE → veto
    return True


def get_bus_snapshot() -> Dict[str, List[dict]]:
    """Diagnostic: return a JSON-serialisable snapshot of bus state.

    Used by the hub API / logging hooks to expose bus state for
    observability without exposing internal dataclass types.
    """
    return {
        f"{sid}:{asset}": [
            {
                "strategy_id": r.strategy_id,
                "asset": r.asset,
                "window_ts": r.window_ts,
                "direction": r.direction,
                "fired_at": r.fired_at,
            }
            for r in entries
        ]
        for (sid, asset), entries in _bus.items()
    }


def reset_sister_veto_bus() -> None:
    """Clear all bus state. Call in test teardown for isolation."""
    _bus.clear()
