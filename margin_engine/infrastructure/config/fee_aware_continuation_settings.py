"""
Settings additions for fee-aware continuation feature.

Add these to margin_engine/infrastructure/config/settings.py MarginSettings class.
"""

# ── Fee-aware continuation (NEW) ──
# Feature flag to enable fee-aware continuation logic
fee_aware_continuation_enabled: bool = False

# Partial take-profit thresholds
fee_aware_partial_tp_threshold: float = 0.5  # 50% of TP distance
fee_aware_partial_tp_size: float = 0.5  # Close 50% of position

# Multi-timescale continuation
continuation_alignment_enabled: bool = False
continuation_min_timescales: int = 2  # Minimum to continue (vs 3 for entry)
continuation_hold_extension_max: float = 2.0  # Max 2.0x hold time

# Signal strength for hold extension
continuation_conviction_min: float = 0.10  # Minimum |p-0.5| to continue
continuation_regime_bonus: bool = True  # Extend hold in TRENDING regimes

# Hold extension multipliers
continuation_extend_at_75_tp: float = 1.0  # No extension at 75% TP
continuation_extend_at_50_tp_weak: float = (
    0.5  # Reduce hold at 50% TP with weak signals
)

# Partial close tracking
max_partial_closes: int = 3  # Maximum partial closes before full exit
partial_close_cooldown_s: float = 300.0  # 5 min between partial closes
