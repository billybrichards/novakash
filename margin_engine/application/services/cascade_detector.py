"""
Cascade state machine for liquidation cascade detection.

Analyzes V4 snapshot cascade data to determine:
- Current cascade state (IDLE, CASCADE, BET, COOLDOWN)
- Cascade direction (LONG or SHORT liquidations)
- Entry quality (PREMIUM, STANDARD, LATE)
- Whether it's safe to fade the cascade
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CascadeState(Enum):
    """Cascade FSM states."""

    IDLE = "IDLE"  # No cascade detected
    CASCADE = "CASCADE"  # Cascade in progress
    BET = "BET"  # Optimal entry point (exhaustion imminent)
    COOLDOWN = "COOLDOWN"  # Wait after cascade ends


@dataclass(frozen=True)
class CascadeInfo:
    """
    Cascade analysis result from V4 snapshot.

    Contains state machine output for cascade fade strategy.
    """

    state: CascadeState
    direction: Optional[str]  # "LONG" or "SHORT" liquidations
    strength: float  # 0-1, higher = stronger cascade
    time_to_exhaustion_s: float  # Seconds until expected exhaustion
    entry_quality: str  # "PREMIUM", "STANDARD", "LATE", "NONE"
    is_safe_to_fade: bool  # True if we should fade now


def analyze_cascade(v4_snapshot: "V4Snapshot") -> CascadeInfo:
    """
    Analyze cascade state from V4 snapshot.

    Args:
        v4_snapshot: V4 snapshot containing cascade data

    Returns:
        CascadeInfo with state, direction, and entry recommendation

    Design notes:
    - Cascade strength >= 0.7 → CASCADE state (strong cascade, imminent exhaustion)
    - Cascade strength 0.5-0.7 → BET state (approaching exhaustion)
    - Cascade strength < 0.3 → IDLE (no cascade or weakening)
    - Direction determined by composite_v3 sign:
      * Positive composite = price up = LONGs getting liquidated
      * Negative composite = price down = SHORTs getting liquidated
    - Entry quality based on strength:
      * >= 0.7 → PREMIUM (strong cascade, high conviction fade)
      * 0.5-0.7 → STANDARD
      * < 0.5 → LATE (cascade weakening, lower conviction)
    - Safe to fade when:
      * State is CASCADE or BET
      * Strength >= 0.5
      * Direction is determined
    """
    ts = v4_snapshot.timescales.get("15m")
    if not ts or not ts.cascade:
        return CascadeInfo(
            state=CascadeState.IDLE,
            direction=None,
            strength=0.0,
            time_to_exhaustion_s=0.0,
            entry_quality="NONE",
            is_safe_to_fade=False,
        )

    cascade = ts.cascade
    strength = cascade.strength or 0.0
    tau1 = cascade.tau1 or 0.0
    tau2 = cascade.tau2 or 0.0
    exhaustion_t = cascade.exhaustion_t or 0.0

    # Determine state based on strength
    if strength < 0.3:
        state = CascadeState.IDLE
    elif strength >= 0.7:
        state = CascadeState.CASCADE
    elif strength >= 0.5:
        state = CascadeState.BET
    else:
        state = CascadeState.COOLDOWN

    # Determine entry quality (NONE when no cascade)
    if strength == 0.0:
        entry_quality = "NONE"
    elif strength >= 0.7:
        entry_quality = "PREMIUM"
    elif strength >= 0.5:
        entry_quality = "STANDARD"
    else:
        entry_quality = "LATE"

    # Determine direction from composite_v3
    # Positive composite = price up = LONGs getting liquidated
    # Negative composite = price down = SHORTs getting liquidated
    composite = ts.composite_v3 or 0.0
    if composite > 0:
        direction = "SHORT"  # Positive composite = price up = LONGs getting liquidated
    elif composite < 0:
        direction = (
            "LONG"  # Negative composite = price down = SHORTs getting liquidated
        )
    else:
        direction = None

    # Safety check for fading
    is_safe = (
        state in (CascadeState.CASCADE, CascadeState.BET)
        and strength >= 0.5
        and direction is not None
    )

    return CascadeInfo(
        state=state,
        direction=direction,
        strength=strength,
        time_to_exhaustion_s=exhaustion_t,
        entry_quality=entry_quality,
        is_safe_to_fade=is_safe,
    )
